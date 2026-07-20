import asyncio
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from .config import settings
from .database import SessionLocal, init_db, session, write_lock
from .models import Audit, Instance, Job, Order, PaymentEvent, Plan, User
from .security import csrf_token, hash_password, read_session, session_token, valid_csrf, verify_password
from .services.clicd import CLICD, plan_payload
from .services.hashpay import HashPay
from .services.mailer import send_mail
from .services.settings import get, set_many

cfg = settings()
root = Path(__file__).parent
templates = Jinja2Templates(root / "templates")
rate_buckets: dict[str, list[float]] = {}


def current(request: Request):
    return read_session(request.cookies.get("vps_session"))


def guard(request: Request, admin: bool = False):
    user = current(request)
    if not user or (admin and not user.get("admin")):
        raise HTTPException(401, "请先登录")
    return user


def check_csrf(request: Request, value: str):
    if not valid_csrf(request.cookies.get("vps_session", ""), value):
        raise HTTPException(419, "CSRF 校验失败")


def limit(request: Request, key: str, maximum: int, window: int = 60):
    now = time.monotonic()
    bucket_key = f"{key}:{request.client.host if request.client else 'unknown'}"
    hits = [stamp for stamp in rate_buckets.get(bucket_key, []) if now - stamp < window]
    if len(hits) >= maximum:
        raise HTTPException(429, "请求过于频繁，请稍后重试")
    hits.append(now)
    rate_buckets[bucket_key] = hits


def ctx(request: Request, **values):
    user = current(request)
    return {
        "request": request,
        "user": user,
        "csrf": csrf_token(request.cookies.get("vps_session", "")) if user else "",
        **values,
    }


async def site_url(db) -> str:
    return (await get(db, "site_url", cfg.base_url)).rstrip("/")


def unwrap(result):
    return result.get("data", result) if isinstance(result, dict) else result


def plan_snapshot(plan: Plan) -> str:
    fields = ["name", "description", "price_cents", "currency", "months", "cpu", "memory_mb", "disk_gb", "traffic_gb", "network_down_mbps", "network_up_mbps", "virtualization", "clicd_image"]
    return json.dumps({field: getattr(plan, field) for field in fields}, ensure_ascii=False)


async def process_job(db, job: Job):
    if job.kind == "provision":
        await provision(db, job.ref_id)
    elif job.kind == "mail_instance":
        instance = await db.get(Instance, job.ref_id)
        order = await db.get(Order, instance.order_id)
        user = await db.get(User, order.user_id)
        await send_mail(db, user.email, f"您的 VPS {instance.name} 已交付", f"实例：{instance.name}\n状态：{instance.status}\nIP：{instance.ip}\nIPv6：{instance.ipv6}\nSSH 端口：{instance.ssh_port}\n到期：{instance.expires_at}\n\n请登录客户中心管理实例并及时修改初始密码。")


async def worker():
    while True:
        try:
            async with SessionLocal() as db:
                async with write_lock:
                    job = (await db.execute(select(Job).where(Job.status == "pending", Job.run_after <= datetime.utcnow()).order_by(Job.id).limit(1))).scalar_one_or_none()
                    if job:
                        job.status = "running"
                        job.locked_at = datetime.utcnow()
                        job.attempts += 1
                        await db.commit()
                if job:
                    try:
                        await process_job(db, job)
                        job.status, job.error = "done", ""
                    except Exception as exc:
                        job.status = "pending" if job.attempts < 5 else "failed"
                        job.error = str(exc)[:1000]
                        job.run_after = datetime.utcnow() + timedelta(seconds=min(900, 2 ** job.attempts * 10))
                    await db.commit()
        except Exception:
            pass
        await asyncio.sleep(2)


async def provision(db, order_id: int):
    order = await db.get(Order, order_id)
    if not order or order.status not in {"paid", "provisioning"}:
        return
    plan = await db.get(Plan, order.plan_id)
    existing = (await db.execute(select(Instance).where(Instance.order_id == order.id))).scalar_one_or_none()
    if existing and existing.clicd_id:
        return
    expires = datetime.utcnow() + timedelta(days=30 * plan.months)
    client = CLICD(await get(db, "clicd_base_url"), await get(db, "clicd_token"))
    order.status = "provisioning"
    await db.commit()
    result = await client.create(plan_payload(plan, order.order_no, expires.isoformat()))
    obj = result.get("data", result)
    instance = existing or Instance(user_id=order.user_id, order_id=order.id, plan_id=plan.id, name=f"VPS-{order.order_no[-8:]}")
    instance.clicd_id = str(obj.get("uuid") or obj.get("id") or "")
    if not instance.clicd_id:
        raise RuntimeError("CLICD 未返回实例 ID")
    instance.status = obj.get("status", "running")
    instance.ip = obj.get("ip", "")
    instance.ipv6 = obj.get("ipv6", "")
    instance.ssh_port = int(obj.get("ssh_port") or 22)
    instance.access_json = json.dumps({"port_mappings": obj.get("port_mappings", [])}, ensure_ascii=False)
    instance.expires_at = expires
    instance.last_synced_at = datetime.utcnow()
    db.add(instance)
    order.status = "fulfilled"
    order.fulfilled_at = datetime.utcnow()
    await db.flush()
    if not (await db.execute(select(Job).where(Job.kind == "mail_instance", Job.ref_id == instance.id))).scalar_one_or_none():
        db.add(Job(kind="mail_instance", ref_id=instance.id))
    await db.commit()


@asynccontextmanager
async def lifespan(app):
    await init_db()
    task = asyncio.create_task(worker())
    yield
    task.cancel()


app = FastAPI(title="VPS-ONE", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=root / "static"), name="static")


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin", "Permissions-Policy": "camera=(), microphone=(), geolocation=()"})
    return response


@app.get("/healthz")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, db=Depends(session)):
    plans = (await db.execute(select(Plan).where(Plan.active.is_(True)).order_by(Plan.sort_order, Plan.price_cents))).scalars().all()
    site = {key: await get(db, key, default) for key, default in {"site_name": "VPS-ONE", "site_tagline": "高性能容器云", "site_footer": "稳定算力，专注增长"}.items()}
    return templates.TemplateResponse("home.html", ctx(request, plans=plans, site=site))


@app.get("/install", response_class=HTMLResponse)
async def install_page(request: Request, db=Depends(session)):
    if await db.scalar(select(func.count(User.id))):
        return RedirectResponse("/")
    return templates.TemplateResponse("install.html", {"request": request})


@app.post("/install")
async def install(email: str = Form(), password: str = Form(), db=Depends(session)):
    if len(password) < 12:
        raise HTTPException(400, "管理员密码至少 12 位")
    async with write_lock:
        if await db.scalar(select(func.count(User.id))):
            raise HTTPException(403)
        db.add(User(email=email.strip().lower(), password_hash=hash_password(password), is_admin=True))
        db.add_all([
            Plan(name="轻量云", slug="starter", description="开发与个人站点", price_cents=1999, cpu=1, memory_mb=1024, disk_gb=20, traffic_gb=500, network_down_mbps=100, network_up_mbps=50),
            Plan(name="标准云", slug="standard", description="企业应用首选", price_cents=3999, cpu=2, memory_mb=2048, disk_gb=40, traffic_gb=1000, network_down_mbps=200, network_up_mbps=100),
            Plan(name="性能云", slug="performance", description="高负载业务", price_cents=7999, cpu=4, memory_mb=4096, disk_gb=80, traffic_gb=2000, network_down_mbps=300, network_up_mbps=150),
        ])
        await db.commit()
    return RedirectResponse("/login", 303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})


@app.post("/login")
async def login(request: Request, email: str = Form(), password: str = Form(), db=Depends(session)):
    limit(request, "login", 8, 300)
    user = (await db.execute(select(User).where(User.email == email.strip().lower()))).scalar_one_or_none()
    if not user or not user.is_active or not verify_password(user.password_hash, password):
        return templates.TemplateResponse("login.html", {"request": request, "error": "邮箱或密码错误"}, status_code=400)
    user.last_login_at = datetime.utcnow()
    await db.commit()
    response = RedirectResponse("/admin" if user.is_admin else "/dashboard", 303)
    response.set_cookie("vps_session", session_token(user.id, user.is_admin), httponly=True, samesite="lax", secure=not cfg.debug, max_age=1209600)
    return response


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": ""})


@app.post("/register")
async def register(request: Request, email: str = Form(), password: str = Form(), db=Depends(session)):
    limit(request, "register", 5, 600)
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return templates.TemplateResponse("register.html", {"request": request, "error": "请输入有效邮箱"}, status_code=400)
    if len(password) < 10 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return templates.TemplateResponse("register.html", {"request": request, "error": "密码至少 10 位，且包含字母和数字"}, status_code=400)
    async with write_lock:
        if (await db.execute(select(User).where(User.email == email))).scalar_one_or_none():
            return templates.TemplateResponse("register.html", {"request": request, "error": "邮箱已注册"}, status_code=409)
        db.add(User(email=email, password_hash=hash_password(password)))
        await db.commit()
    return RedirectResponse("/login", 303)


@app.post("/logout")
async def logout():
    response = RedirectResponse("/", 303)
    response.delete_cookie("vps_session")
    return response


@app.post("/orders")
async def create_order(request: Request, plan_id: int = Form(), csrf: str = Form(), db=Depends(session)):
    user = guard(request)
    check_csrf(request, csrf)
    limit(request, "order", 10, 300)
    plan = await db.get(Plan, plan_id)
    if not plan or not plan.active or plan.stock == 0:
        raise HTTPException(404, "套餐不可购买")
    order_no = "VP" + datetime.utcnow().strftime("%Y%m%d%H%M%S") + secrets.token_hex(3).upper()
    order = Order(order_no=order_no, user_id=user["uid"], plan_id=plan.id, plan_snapshot=plan_snapshot(plan), amount_cents=plan.price_cents, currency=plan.currency)
    db.add(order)
    await db.commit()
    await db.refresh(order)
    try:
        base, merchant, private_key = await get(db, "hashpay_base_url"), await get(db, "hashpay_merchant_id"), await get(db, "hashpay_private_key")
        public_url = await site_url(db)
        result = await HashPay(base, merchant, private_key).create({"merchantNo": order_no, "amount": f"{plan.price_cents / 100:.2f}", "currency": plan.currency, "description": plan.name, "notify_url": public_url + "/hashpay/callback", "return_url": public_url + "/dashboard"})
        data = result.get("data") or result.get("order") or result
        order.hashpay_id = str(data.get("id") or data.get("orderId") or "") or None
        order.checkout_url = result.get("checkoutUrl") or data.get("checkoutUrl") or data.get("payUrl")
        order.status = "payment_pending"
        await db.commit()
    except Exception as exc:
        order.status = "payment_error"
        await db.commit()
        raise HTTPException(502, f"支付订单创建失败：{exc}") from exc
    return RedirectResponse(order.checkout_url or "/dashboard", 303)


@app.post("/hashpay/callback")
async def callback(request: Request, db=Depends(session)):
    limit(request, "callback", 120)
    raw = await request.json()
    private_key = await get(db, "hashpay_private_key")
    merchant_id = await get(db, "hashpay_merchant_id")
    if request.headers.get("X-HashPay-Merchant") != merchant_id:
        raise HTTPException(401, "HashPay 商户标识不匹配")
    try:
        payload = HashPay("", "", private_key).decrypt_callback(raw)
    except Exception as exc:
        raise HTTPException(400, "HashPay 回调解密失败") from exc
    order_no = str(payload.get("merchantNo") or "")
    event_id = str(payload.get("eventId") or payload.get("id") or secrets.token_hex(16))
    order = (await db.execute(select(Order).where(Order.order_no == order_no))).scalar_one_or_none()
    if not order:
        return JSONResponse({"error": "order not found"}, 404)
    async with write_lock:
        if (await db.execute(select(PaymentEvent).where(PaymentEvent.event_id == event_id))).scalar_one_or_none():
            return {"ok": True}
        amount = round(float(payload.get("amount", 0)) * 100)
        status = str(payload.get("status", "")).lower()
        verified = amount == order.amount_cents and status in {"paid", "success", "completed"}
        db.add(PaymentEvent(event_id=event_id, order_no=order_no, platform_txn_id=str(payload.get("transactionId") or ""), verified=verified, payload=json.dumps(payload, ensure_ascii=False)))
        if not verified:
            await db.commit()
            raise HTTPException(400, "支付数据不匹配")
        if order.status not in {"paid", "provisioning", "fulfilled"}:
            order.status, order.paid_at = "paid", datetime.utcnow()
            db.add(Job(kind="provision", ref_id=order.id))
        await db.commit()
    return {"ok": True}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db=Depends(session)):
    user = guard(request)
    orders = (await db.execute(select(Order).where(Order.user_id == user["uid"]).order_by(Order.id.desc()).limit(100))).scalars().all()
    instances = (await db.execute(select(Instance).where(Instance.user_id == user["uid"]).order_by(Instance.id.desc()))).scalars().all()
    return templates.TemplateResponse("dashboard.html", ctx(request, orders=orders, instances=instances))


@app.post("/instances/{instance_id}/{action}")
async def instance_action(instance_id: int, action: str, request: Request, csrf: str = Form(), template_id: str = Form(""), db=Depends(session)):
    user = guard(request)
    check_csrf(request, csrf)
    limit(request, "instance", 20)
    instance = await db.get(Instance, instance_id)
    if not instance or instance.user_id != user["uid"]:
        raise HTTPException(404)
    allowed = {"start", "stop", "restart", "reset-password", "reinstall"}
    if action not in allowed:
        raise HTTPException(400, "不允许的操作")
    payload = {"template_id": template_id, "ssh_auth_mode": "keep"} if action == "reinstall" and template_id else {}
    client = CLICD(await get(db, "clicd_base_url"), await get(db, "clicd_token"))
    await client.action(instance.clicd_id, action, payload)
    db.add(Audit(user_id=user["uid"], action="instance." + action, detail=str(instance_id), ip=request.client.host if request.client else ""))
    await db.commit()
    return RedirectResponse("/dashboard", 303)


@app.post("/instances/{instance_id}/snapshot")
async def snapshot(instance_id: int, request: Request, csrf: str = Form(), name: str = Form(), db=Depends(session)):
    user = guard(request)
    check_csrf(request, csrf)
    instance = await db.get(Instance, instance_id)
    if not instance or instance.user_id != user["uid"]:
        raise HTTPException(404)
    await CLICD(await get(db, "clicd_base_url"), await get(db, "clicd_token")).create_snapshot(instance.clicd_id, name[:64])
    db.add(Audit(user_id=user["uid"], action="instance.snapshot", detail=name[:64]))
    await db.commit()
    return RedirectResponse("/dashboard", 303)


@app.post("/instances/{instance_id}/port")
async def add_port(instance_id: int, request: Request, csrf: str = Form(), protocol: str = Form(), host_port: int = Form(), container_port: int = Form(), description: str = Form(""), db=Depends(session)):
    user = guard(request)
    check_csrf(request, csrf)
    instance = await db.get(Instance, instance_id)
    if not instance or instance.user_id != user["uid"] or protocol not in {"tcp", "udp"} or not (1 <= host_port <= 65535 and 1 <= container_port <= 65535):
        raise HTTPException(400)
    await CLICD(await get(db, "clicd_base_url"), await get(db, "clicd_token")).add_port(instance.clicd_id, {"protocol": protocol, "host_port": host_port, "container_port": container_port, "description": description[:100]})
    db.add(Audit(user_id=user["uid"], action="instance.port.create", detail=f"{host_port}:{container_port}"))
    await db.commit()
    return RedirectResponse("/dashboard", 303)


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, db=Depends(session)):
    guard(request, True)
    models = {"用户": User, "套餐": Plan, "订单": Order, "实例": Instance, "任务": Job}
    stats = {name: await db.scalar(select(func.count(model.id))) for name, model in models.items()}
    orders = (await db.execute(select(Order).order_by(Order.id.desc()).limit(30))).scalars().all()
    jobs = (await db.execute(select(Job).order_by(Job.id.desc()).limit(30))).scalars().all()
    return templates.TemplateResponse("admin.html", ctx(request, stats=stats, orders=orders, jobs=jobs))


@app.get("/admin/products", response_class=HTMLResponse)
async def admin_products(request: Request, db=Depends(session)):
    guard(request, True)
    error = ""
    dashboard_data, containers, host, routing, tasks, security = {}, [], {}, {}, [], {}
    try:
        client = CLICD(await get(db, "clicd_base_url"), await get(db, "clicd_token"))
        results = await asyncio.gather(client.dashboard(), client.containers(), client.host_info(), client.routing(), client.tasks(), client.security_summary())
        dashboard_data, containers, host, routing, tasks, security = [unwrap(item) for item in results]
    except Exception as exc:
        error = str(exc)
    audits = (await db.execute(select(Audit).where(Audit.action.like("admin.clicd.%")).order_by(Audit.id.desc()).limit(30))).scalars().all()
    return templates.TemplateResponse("admin_products.html", ctx(request, dashboard=dashboard_data or {}, containers=containers or [], host=host or {}, routing=routing or {}, tasks=tasks or [], security=security or {}, audits=audits, error=error))


@app.post("/admin/products/{container_id}/{action}")
async def admin_product_action(container_id: str, action: str, request: Request, csrf: str = Form(), db=Depends(session)):
    user = guard(request, True)
    check_csrf(request, csrf)
    allowed = {"start", "stop", "restart", "reset-password"}
    if action not in allowed:
        raise HTTPException(400, "不允许的 CLICD 操作")
    client = CLICD(await get(db, "clicd_base_url"), await get(db, "clicd_token"))
    await client.action(container_id, action)
    db.add(Audit(user_id=user["uid"], action=f"admin.clicd.{action}", detail=container_id, ip=request.client.host if request.client else ""))
    await db.commit()
    return RedirectResponse("/admin/products", 303)


@app.post("/admin/products/{container_id}/limits")
async def admin_product_limits(container_id: str, request: Request, csrf: str = Form(), vcpu: int = Form(), ram_mb: int = Form(), network_down_mbps: int = Form(), network_up_mbps: int = Form(), io_read_mbps: int = Form(0), io_write_mbps: int = Form(0), monthly_traffic_gb: int = Form(0), db=Depends(session)):
    user = guard(request, True)
    check_csrf(request, csrf)
    if min(vcpu, ram_mb, network_down_mbps, network_up_mbps) < 0:
        raise HTTPException(400, "资源限制无效")
    client = CLICD(await get(db, "clicd_base_url"), await get(db, "clicd_token"))
    await client.update_resource_limit(container_id, {"vcpu": vcpu, "ram_mb": ram_mb, "network_down_mbps": network_down_mbps, "network_up_mbps": network_up_mbps, "io_read_mbps": io_read_mbps, "io_write_mbps": io_write_mbps})
    await client.update_traffic_limit(container_id, {"traffic_mode": "total", "monthly_traffic_gb": monthly_traffic_gb})
    db.add(Audit(user_id=user["uid"], action="admin.clicd.limits", detail=container_id))
    await db.commit()
    return RedirectResponse("/admin/products", 303)


@app.post("/admin/products/{container_id}/delete")
async def admin_product_delete(container_id: str, request: Request, csrf: str = Form(), confirmation: str = Form(), db=Depends(session)):
    user = guard(request, True)
    check_csrf(request, csrf)
    if confirmation != container_id:
        raise HTTPException(400, "请输入完整容器 ID 确认删除")
    await CLICD(await get(db, "clicd_base_url"), await get(db, "clicd_token")).delete(container_id)
    db.add(Audit(user_id=user["uid"], action="admin.clicd.delete", detail=container_id, ip=request.client.host if request.client else ""))
    await db.commit()
    return RedirectResponse("/admin/products", 303)


@app.get("/admin/plans", response_class=HTMLResponse)
async def admin_plans(request: Request, db=Depends(session)):
    guard(request, True)
    plans = (await db.execute(select(Plan).order_by(Plan.sort_order, Plan.id))).scalars().all()
    templates_list, error = [], ""
    try:
        templates_list = unwrap(await CLICD(await get(db, "clicd_base_url"), await get(db, "clicd_token")).templates()) or []
    except Exception as exc:
        error = str(exc)
    return templates.TemplateResponse("admin_plans.html", ctx(request, plans=plans, templates_list=templates_list, error=error))


@app.post("/admin/plans")
async def save_plan(request: Request, csrf: str = Form(), plan_id: int = Form(0), name: str = Form(), slug: str = Form(), description: str = Form(""), price_cents: int = Form(), months: int = Form(1), stock: int = Form(-1), cpu: int = Form(), memory_mb: int = Form(), disk_gb: int = Form(), traffic_gb: int = Form(), network_down_mbps: int = Form(), network_up_mbps: int = Form(), virtualization: str = Form("lxc"), clicd_image: str = Form(), assign_nat: bool = Form(False), assign_ipv4: bool = Form(False), assign_ipv6: bool = Form(False), active: bool = Form(False), db=Depends(session)):
    user = guard(request, True)
    check_csrf(request, csrf)
    if virtualization not in {"lxc", "kvm"} or min(price_cents, months, cpu, memory_mb, disk_gb) < 1:
        raise HTTPException(400, "套餐字段无效")
    client = CLICD(await get(db, "clicd_base_url"), await get(db, "clicd_token"))
    images = unwrap(await client.templates(virtualization)) or []
    matched = next((item for item in images if str(item.get("id") or item.get("template_id") or item.get("slug")) == clicd_image), None)
    if not matched:
        raise HTTPException(400, "CLICD 中未找到已启用且已下载的对应镜像")
    plan = await db.get(Plan, plan_id) if plan_id else Plan(name=name, slug=slug, price_cents=price_cents, cpu=cpu, memory_mb=memory_mb, disk_gb=disk_gb)
    for key, value in {"name": name, "slug": slug, "description": description, "price_cents": price_cents, "months": months, "stock": stock, "cpu": cpu, "memory_mb": memory_mb, "disk_gb": disk_gb, "traffic_gb": traffic_gb, "network_down_mbps": network_down_mbps, "network_up_mbps": network_up_mbps, "virtualization": virtualization, "clicd_image": clicd_image, "clicd_template_name": str(matched.get("name") or matched.get("label") or clicd_image), "clicd_validated_at": datetime.utcnow(), "assign_nat": assign_nat, "assign_ipv4": assign_ipv4, "assign_ipv6": assign_ipv6, "active": active}.items():
        setattr(plan, key, value)
    db.add(plan)
    db.add(Audit(user_id=user["uid"], action="plan.save", detail=slug))
    await db.commit()
    return RedirectResponse("/admin/plans", 303)


@app.post("/admin/plans/{plan_id}/toggle")
async def toggle_plan(plan_id: int, request: Request, csrf: str = Form(), db=Depends(session)):
    guard(request, True)
    check_csrf(request, csrf)
    plan = await db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(404)
    plan.active = not plan.active
    await db.commit()
    return RedirectResponse("/admin/plans", 303)


@app.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db=Depends(session)):
    guard(request, True)
    keys = ["site_name", "site_tagline", "site_footer", "site_url", "clicd_base_url", "hashpay_base_url", "hashpay_merchant_id", "smtp_host", "smtp_port", "smtp_security", "smtp_username", "smtp_from"]
    values = {key: await get(db, key) for key in keys}
    return templates.TemplateResponse("settings.html", ctx(request, values=values, saved=request.query_params.get("saved")))


@app.post("/admin/settings")
async def settings_save(request: Request, csrf: str = Form(), db=Depends(session)):
    user = guard(request, True)
    form = await request.form()
    check_csrf(request, csrf)
    allowed = {"site_name", "site_tagline", "site_footer", "site_url", "clicd_base_url", "clicd_token", "hashpay_base_url", "hashpay_merchant_id", "hashpay_private_key", "hashpay_public_key", "smtp_host", "smtp_port", "smtp_security", "smtp_username", "smtp_password", "smtp_from"}
    values = {key: str(value).strip() for key, value in form.items() if key in allowed}
    for key in {"site_url", "clicd_base_url", "hashpay_base_url"} & values.keys():
        parsed = urlparse(values[key])
        if values[key] and parsed.scheme not in {"http", "https"}:
            raise HTTPException(400, "接口地址必须使用 HTTP 或 HTTPS")
    secret_keys = {"clicd_token", "hashpay_private_key", "hashpay_public_key", "smtp_password"}
    await set_many(db, values, secret_keys)
    db.add(Audit(user_id=user["uid"], action="settings.update"))
    await db.commit()
    return RedirectResponse("/admin/settings?saved=1", 303)


@app.post("/admin/settings/test/{service}")
async def test_service(service: str, request: Request, csrf: str = Form(), recipient: str = Form(""), db=Depends(session)):
    guard(request, True)
    check_csrf(request, csrf)
    if service == "clicd":
        await CLICD(await get(db, "clicd_base_url"), await get(db, "clicd_token")).test()
    elif service == "smtp":
        await send_mail(db, recipient, "VPS-ONE SMTP 测试", "邮件配置工作正常。")
    elif service == "hashpay":
        base = await get(db, "hashpay_base_url")
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(base)
            response.raise_for_status()
    else:
        raise HTTPException(404)
    return RedirectResponse("/admin/settings?saved=test-ok", 303)

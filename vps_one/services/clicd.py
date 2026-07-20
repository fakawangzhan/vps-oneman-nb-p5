from typing import Any
import httpx


class CLICDError(RuntimeError):
    pass


class CLICD:
    def __init__(self, base_url: str, token: str):
        if not base_url or not token:
            raise CLICDError("CLICD 尚未配置")
        self.base = base_url.rstrip("/")
        self.headers = {"X-API-Key": token, "Content-Type": "application/json"}

    async def request(self, method: str, path: str, data: dict[str, Any] | None = None, params: dict[str, Any] | None = None):
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5)) as client:
            response = await client.request(method, self.base + "/api/v1" + path, headers=self.headers, json=data, params=params)
        try:
            response.raise_for_status()
            result = response.json() if response.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            try:
                detail = response.json()
                message = detail.get("message") or detail.get("detail") or response.text
            except ValueError:
                message = response.text
            raise CLICDError(f"CLICD 请求失败：{response.status_code} · {str(message)[:800]}") from exc
        if isinstance(result, dict) and result.get("success") is False:
            raise CLICDError(str(result.get("message") or "CLICD 操作失败"))
        return result

    async def test(self):
        return await self.request("GET", "/dashboard")

    async def dashboard(self):
        return await self.request("GET", "/dashboard")

    async def containers(self):
        return await self.request("GET", "/containers")

    async def templates(self, virtualization: str = ""):
        return await self.request("GET", "/images/enabled", params={"type": virtualization} if virtualization else None)

    async def host_info(self):
        return await self.request("GET", "/host-info")

    async def routing(self):
        return await self.request("GET", "/routing")

    async def tasks(self):
        return await self.request("GET", "/tasks")

    async def snapshots_overview(self):
        return await self.request("GET", "/snapshots")

    async def security_summary(self):
        return await self.request("GET", "/security/summary")

    async def audit_logs(self):
        return await self.request("GET", "/audit-logs")

    async def create(self, payload: dict[str, Any]):
        return await self.request("POST", "/containers", payload)

    async def delete(self, instance_id: str):
        return await self.request("DELETE", f"/containers/{instance_id}/delete")

    async def update_resource_limit(self, instance_id: str, payload: dict[str, Any]):
        return await self.request("PUT", f"/containers/{instance_id}/resource-limit", payload)

    async def update_traffic_limit(self, instance_id: str, payload: dict[str, Any]):
        return await self.request("PUT", f"/containers/{instance_id}/traffic-limit", payload)

    async def update_expiry(self, instance_id: str, expires_at: str):
        return await self.request("PUT", f"/containers/{instance_id}/expiry", {"expires_at": expires_at})

    async def get(self, instance_id: str):
        return await self.request("GET", f"/containers/{instance_id}")

    async def usage(self, instance_id: str):
        return await self.request("GET", f"/containers/{instance_id}/usage")

    async def action(self, instance_id: str, action: str, data: dict[str, Any] | None = None):
        allowed = {"start", "stop", "restart", "reset-password", "reinstall"}
        if action not in allowed:
            raise CLICDError("不允许的实例操作")
        return await self.request("POST", f"/containers/{instance_id}/{action}", data or {})

    async def snapshots(self, instance_id: str):
        return await self.request("GET", f"/containers/{instance_id}/snapshots")

    async def create_snapshot(self, instance_id: str, name: str):
        return await self.request("POST", f"/containers/{instance_id}/snapshots", {"name": name})

    async def add_port(self, instance_id: str, payload: dict[str, Any]):
        return await self.request("POST", f"/containers/{instance_id}/port-mappings", payload)

    async def firewall(self, instance_id: str, payload: dict[str, Any] | None = None):
        method = "PUT" if payload is not None else "GET"
        return await self.request(method, f"/containers/{instance_id}/firewall", payload)

    async def ssh_ticket(self, instance_id: str):
        return await self.request("POST", "/ssh-ticket", {"container_id": instance_id})


def plan_payload(plan, order_no: str, expires_at: str) -> dict[str, Any]:
    return {
        "name": f"vps-{order_no.lower()}",
        "virtualization": plan.virtualization,
        "template_id": plan.clicd_image,
        "vcpu": plan.cpu,
        "ram_mb": plan.memory_mb,
        "disk_gb": plan.disk_gb,
        "assign_nat": plan.assign_nat,
        "port_mapping_count": plan.port_mapping_count,
        "assign_ipv4": plan.assign_ipv4,
        "ipv4_count": plan.ipv4_count,
        "public_ipv4s": [],
        "assign_ipv6": plan.assign_ipv6,
        "ipv6_count": plan.ipv6_count,
        "ipv6_addresses": [],
        "ssh_auth_mode": "auto_password",
        "ssh_password": "",
        "ssh_public_key": "",
        "expires_at": expires_at,
        "network_down_mbps": plan.network_down_mbps,
        "network_up_mbps": plan.network_up_mbps,
        "io_read_mbps": plan.io_read_mbps,
        "io_write_mbps": plan.io_write_mbps,
    }

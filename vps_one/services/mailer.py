import asyncio
import smtplib
from email.message import EmailMessage
from sqlalchemy.ext.asyncio import AsyncSession
from .settings import get


async def send_mail(db: AsyncSession, recipient: str, subject: str, text: str) -> None:
    host = await get(db, "smtp_host")
    port = int(await get(db, "smtp_port", "587"))
    username = await get(db, "smtp_username")
    password = await get(db, "smtp_password")
    sender = await get(db, "smtp_from") or username
    mode = await get(db, "smtp_security", "starttls")
    if not host or not sender:
        raise RuntimeError("SMTP 尚未配置")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text)

    def deliver():
        client_class = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
        with client_class(host, port, timeout=15) as client:
            if mode == "starttls":
                client.starttls()
            if username:
                client.login(username, password)
            client.send_message(message)

    await asyncio.to_thread(deliver)

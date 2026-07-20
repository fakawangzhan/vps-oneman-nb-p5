import hashlib,hmac,secrets,time
from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from itsdangerous import URLSafeTimedSerializer,BadSignature
from .config import settings
ph=PasswordHasher(); cfg=settings(); signer=URLSafeTimedSerializer(cfg.secret_key,salt="session")
def hash_password(v:str)->str:return ph.hash(v)
def verify_password(h:str,v:str)->bool:
    try:return ph.verify(h,v)
    except Exception:return False
def session_token(uid:int,admin:bool)->str:return signer.dumps({"uid":uid,"admin":admin})
def read_session(token:str|None):
    try:return signer.loads(token,max_age=86400*14) if token else None
    except BadSignature:return None
def csrf_token(token:str)->str:return hmac.new(cfg.secret_key.encode(),token.encode(),hashlib.sha256).hexdigest()
def valid_csrf(token:str,provided:str)->bool:return bool(provided and hmac.compare_digest(csrf_token(token),provided))
def fernet():return Fernet(__import__('base64').urlsafe_b64encode(hashlib.sha256(cfg.master_key.encode()).digest()))
def encrypt(v:str)->str:return fernet().encrypt(v.encode()).decode()
def decrypt(v:str)->str:return fernet().decrypt(v.encode()).decode()

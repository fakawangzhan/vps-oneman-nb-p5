from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..models import Setting
from ..security import decrypt,encrypt

async def get(db:AsyncSession,key:str,default=''):
 row=await db.get(Setting,key)
 if not row:return default
 return decrypt(row.value) if row.encrypted and row.value else row.value
async def set_many(db:AsyncSession,values:dict[str,str],secret_keys:set[str]):
 for k,v in values.items():
  row=await db.get(Setting,k) or Setting(key=k)
  if v: row.value=encrypt(v) if k in secret_keys else v
  row.encrypted=k in secret_keys;db.add(row)
 await db.commit()

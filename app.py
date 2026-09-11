
import os, secrets, hashlib, shutil, subprocess, tempfile, base64
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import jwt, httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.background import BackgroundTask
from pydantic import BaseModel
from sqlalchemy import create_engine, String, Integer, Boolean, DateTime, ForeignKey, Float, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, Session

UTC=timezone.utc
JWT_SECRET=os.getenv("JWT_SECRET","f2LkPdO4wamyuuSB-W_7z3fVA9z3CCzTs0l0_fPL8yNAt5mVeQo4IoZwhZba5QAhThS5D_qX7gg2lpd7LX2lMg")
LICENSE_SIGNING_SECRET=os.getenv("LICENSE_SIGNING_SECRET","qE4rET7sQRCC_IjteVWviM6OJ0Zen4RI_Ze3uCV7Qzs1Llxo77uDcQHvJiDUIdo7-knj7PNj3A9R6j6Nn6kTVg")
CONFIG_ENCRYPTION_KEY=os.getenv("CONFIG_ENCRYPTION_KEY","bK2htfDajujtRMMzGVPA2yR7IW34O7KLYHJEHmVmWPw=")
DATABASE_URL=os.getenv("DATABASE_URL","sqlite:///./dev.db")
FRONTEND_URL=os.getenv("FRONTEND_URL","https://lovepdf.free.nf").rstrip("/")
PUBLIC_API_URL=os.getenv("PUBLIC_API_URL","https://lovepdf-compressor-api.onrender.com").rstrip("/")
TRIAL_DAYS=int(os.getenv("TRIAL_DAYS","3"))
TRIAL_CREDITS=int(os.getenv("TRIAL_CREDITS","50"))
MAX_FILE_MB=int(os.getenv("MAX_FILE_MB","50"))

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL=DATABASE_URL.replace("postgres://","postgresql+psycopg://",1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL=DATABASE_URL.replace("postgresql://","postgresql+psycopg://",1)

connect_args={"check_same_thread":False} if DATABASE_URL.startswith("sqlite") else {}
engine=create_engine(DATABASE_URL,pool_pre_ping=True,connect_args=connect_args)
SessionLocal=sessionmaker(bind=engine,autoflush=False,autocommit=False)

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__="users"
    id:Mapped[int]=mapped_column(primary_key=True)
    email:Mapped[str]=mapped_column(String(255),unique=True,index=True)
    name:Mapped[str]=mapped_column(String(120),default="")
    role:Mapped[str]=mapped_column(String(20),default="vendor")
    is_active:Mapped[bool]=mapped_column(Boolean,default=True)
    plan_code:Mapped[str]=mapped_column(String(80),default="trial")
    credits_remaining:Mapped[int]=mapped_column(Integer,default=TRIAL_CREDITS)
    license_expires_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(UTC))

class ApiKey(Base):
    __tablename__="api_keys"
    id:Mapped[int]=mapped_column(primary_key=True)
    user_id:Mapped[int]=mapped_column(ForeignKey("users.id"),index=True)
    name:Mapped[str]=mapped_column(String(120),default="Production")
    prefix:Mapped[str]=mapped_column(String(24),index=True)
    key_hash:Mapped[str]=mapped_column(String(64),unique=True,index=True)
    is_active:Mapped[bool]=mapped_column(Boolean,default=True)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(UTC))
    last_used_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)

class Usage(Base):
    __tablename__="usage"
    id:Mapped[int]=mapped_column(primary_key=True)
    user_id:Mapped[int]=mapped_column(ForeignKey("users.id"),index=True)
    api_key_id:Mapped[int]=mapped_column(ForeignKey("api_keys.id"),index=True)
    input_bytes:Mapped[int]=mapped_column(Integer)
    output_bytes:Mapped[int]=mapped_column(Integer)
    target_kb:Mapped[int]=mapped_column(Integer)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(UTC))

class PaymentConfig(Base):
    __tablename__="payment_config"
    id:Mapped[int]=mapped_column(primary_key=True,default=1)
    mode:Mapped[str]=mapped_column(String(20),default="sandbox")
    consumer_key_enc:Mapped[Optional[str]]=mapped_column(Text,nullable=True)
    consumer_secret_enc:Mapped[Optional[str]]=mapped_column(Text,nullable=True)
    notification_id:Mapped[Optional[str]]=mapped_column(String(190),nullable=True)

class Payment(Base):
    __tablename__="payments"
    id:Mapped[int]=mapped_column(primary_key=True)
    user_id:Mapped[int]=mapped_column(ForeignKey("users.id"),index=True)
    email:Mapped[str]=mapped_column(String(255),index=True)
    local_order_id:Mapped[int]=mapped_column(Integer,index=True)
    merchant_reference:Mapped[str]=mapped_column(String(80),unique=True,index=True)
    package:Mapped[str]=mapped_column(String(80))
    credits:Mapped[int]=mapped_column(Integer)
    days:Mapped[int]=mapped_column(Integer)
    amount:Mapped[float]=mapped_column(Float)
    currency:Mapped[str]=mapped_column(String(10))
    tracking_id:Mapped[Optional[str]]=mapped_column(String(190),nullable=True,index=True)
    redirect_url:Mapped[Optional[str]]=mapped_column(Text,nullable=True)
    status:Mapped[str]=mapped_column(String(30),default="PENDING")
    confirmation_code:Mapped[Optional[str]]=mapped_column(String(190),nullable=True)
    applied_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(UTC))

Base.metadata.create_all(engine)

fernet=Fernet(CONFIG_ENCRYPTION_KEY.encode())
bearer=HTTPBearer(auto_error=False)

def now(): return datetime.now(UTC)

def db_session():
    db=SessionLocal()
    try: yield db
    finally: db.close()

def enc(v:str)->str: return fernet.encrypt(v.encode()).decode()
def dec(v:Optional[str])->str: return fernet.decrypt(v.encode()).decode() if v else ""

def dashboard_token(u:User)->str:
    return jwt.encode({"sub":str(u.id),"role":u.role,"exp":now()+timedelta(hours=12)},JWT_SECRET,algorithm="HS256")

def current_user(cred:Optional[HTTPAuthorizationCredentials]=Depends(bearer),db:Session=Depends(db_session)):
    if not cred: raise HTTPException(401,"Missing dashboard token")
    try:
        p=jwt.decode(cred.credentials,JWT_SECRET,algorithms=["HS256"])
        u=db.get(User,int(p["sub"]))
    except Exception:
        raise HTTPException(401,"Invalid or expired dashboard token")
    if not u or not u.is_active: raise HTTPException(403,"Account disabled")
    return u

def owner_user(u:User=Depends(current_user)):
    if u.role!="owner": raise HTTPException(403,"Owner access required")
    return u

def verify_shared(token:str,audience:str):
    try:
        return jwt.decode(token,LICENSE_SIGNING_SECRET,algorithms=["HS256"],audience=audience)
    except jwt.ExpiredSignatureError:
        raise HTTPException(400,"Signed token expired")
    except Exception:
        raise HTTPException(400,"Invalid signed token")

def hash_key(raw:str)->str: return hashlib.sha256(raw.encode()).hexdigest()

def make_api_key():
    raw="lpdf_"+secrets.token_urlsafe(32)
    return raw,raw[:14],hash_key(raw)

def api_auth(x_api_key:str=Header(...,alias="X-API-Key"),db:Session=Depends(db_session)):
    row=db.scalar(select(ApiKey).where(ApiKey.key_hash==hash_key(x_api_key),ApiKey.is_active==True))
    if not row: raise HTTPException(401,"Invalid API key")
    u=db.get(User,row.user_id)
    if not u or not u.is_active: raise HTTPException(403,"Account disabled")
    if not u.license_expires_at or u.license_expires_at<=now():
        raise HTTPException(402,{"message":"License expired","reason":"expired"})
    if u.credits_remaining<=0:
        raise HTTPException(402,{"message":"Credits exhausted","reason":"credits_exhausted"})
    return u,row

app=FastAPI(title="LovePDF Compressor API",version="3.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Credits-Remaining","X-License-Expires","X-Target-Reached"]
)

class SsoIn(BaseModel): sso_token:str
class KeyIn(BaseModel): name:str="Production"
class EmailIn(BaseModel): email:str
class PurchaseIn(BaseModel): purchase_token:str
class PaymentConfigIn(BaseModel):
    mode:str="sandbox"
    consumer_key:str=""
    consumer_secret:str=""

@app.get("/")
def root():
    return {"service":"LovePDF Compressor API","status":"online","docs":"/docs","health":"/health"}

@app.get("/health")
def health():
    return {"ok":True,"service":"LovePDF Compressor API","version":"3.0.0"}

@app.post("/auth/sso")
def sso(data:SsoIn,db:Session=Depends(db_session)):
    p=verify_shared(data.sso_token,"lovepdf-render-sso")
    if p.get("action")!="dashboard_login": raise HTTPException(400,"Invalid SSO action")
    email=str(p.get("email","")).lower().strip()
    name=str(p.get("name","")).strip()[:120]
    role="owner" if p.get("role")=="owner" else "vendor"
    if not email or "@" not in email: raise HTTPException(400,"Invalid email")

    u=db.scalar(select(User).where(User.email==email))
    created=False
    if not u:
        created=True
        u=User(
            email=email,name=name or email.split("@")[0],role=role,is_active=True,
            plan_code="trial" if role=="vendor" else "owner",
            credits_remaining=TRIAL_CREDITS if role=="vendor" else 999999999,
            license_expires_at=now()+timedelta(days=TRIAL_DAYS) if role=="vendor" else now()+timedelta(days=36500)
        )
        db.add(u);db.commit();db.refresh(u)
    else:
        if role=="owner" and u.role!="owner": u.role="owner"
        if name: u.name=name
        db.commit()
    if not u.is_active: raise HTTPException(403,"Render account disabled")
    return {"token":dashboard_token(u),"created":created,"email":u.email}

@app.get("/me")
def me(u:User=Depends(current_user)):
    active=bool(u.license_expires_at and u.license_expires_at>now() and u.credits_remaining>0)
    return {
        "email":u.email,"name":u.name,"role":u.role,"plan_code":u.plan_code,
        "credits_remaining":u.credits_remaining,"license_expires_at":u.license_expires_at,
        "license_active":active
    }

@app.get("/keys")
def keys(u:User=Depends(current_user),db:Session=Depends(db_session)):
    rows=db.scalars(select(ApiKey).where(ApiKey.user_id==u.id).order_by(ApiKey.id.desc())).all()
    return [{
        "id":r.id,"name":r.name,"prefix":r.prefix,"active":r.is_active,
        "created_at":r.created_at,"last_used_at":r.last_used_at
    } for r in rows]

@app.post("/keys")
def create_key(data:KeyIn,u:User=Depends(current_user),db:Session=Depends(db_session)):
    if u.role!="vendor": raise HTTPException(403,"Vendor account required")
    name=(data.name or "Production").strip()[:120] or "Production"
    for _ in range(5):
        raw,prefix,h=make_api_key()
        if db.scalar(select(ApiKey).where(ApiKey.key_hash==h)): continue
        row=ApiKey(user_id=u.id,name=name,prefix=prefix,key_hash=h,is_active=True)
        db.add(row);db.commit();db.refresh(row)
        return {"id":row.id,"api_key":raw,"prefix":prefix,"warning":"Copy now. It will not be shown again."}
    raise HTTPException(500,"Unable to generate unique API key")

@app.delete("/keys/{key_id}")
def revoke_key(key_id:int,u:User=Depends(current_user),db:Session=Depends(db_session)):
    row=db.get(ApiKey,key_id)
    if not row or row.user_id!=u.id: raise HTTPException(404,"API key not found")
    row.is_active=False;db.commit()
    return {"ok":True,"revoked_key_id":key_id}

@app.get("/usage")
def usage(u:User=Depends(current_user),db:Session=Depends(db_session)):
    rows=db.scalars(select(Usage).where(Usage.user_id==u.id).order_by(Usage.id.desc()).limit(200)).all()
    return [{"input_bytes":r.input_bytes,"output_bytes":r.output_bytes,"target_kb":r.target_kb,"created_at":r.created_at} for r in rows]

@app.post("/admin/vendors/disable")
def disable_vendor(data:EmailIn,owner:User=Depends(owner_user),db:Session=Depends(db_session)):
    u=db.scalar(select(User).where(User.email==data.email.lower().strip()))
    if not u: return {"ok":True,"vendor":"not_found"}
    u.is_active=False
    rows=db.scalars(select(ApiKey).where(ApiKey.user_id==u.id,ApiKey.is_active==True)).all()
    for r in rows:r.is_active=False
    db.commit()
    return {"ok":True,"revoked_keys":len(rows)}

def payment_config(db:Session):
    cfg=db.get(PaymentConfig,1)
    if not cfg:
        cfg=PaymentConfig(id=1,mode="sandbox")
        db.add(cfg);db.commit();db.refresh(cfg)
    return cfg

def pesapal_base(mode:str)->str:
    return "https://pay.pesapal.com/v3" if mode=="live" else "https://cybqa.pesapal.com/pesapalv3"

def pesapal_token(cfg:PaymentConfig):
    key,secret=dec(cfg.consumer_key_enc),dec(cfg.consumer_secret_enc)
    if not key or not secret: raise HTTPException(400,"PesaPal credentials are not configured")
    try:
        r=httpx.post(
            pesapal_base(cfg.mode)+"/api/Auth/RequestToken",
            json={"consumer_key":key,"consumer_secret":secret},
            headers={"Accept":"application/json","Content-Type":"application/json"},
            timeout=25
        )
        data=r.json()
    except Exception as e:
        raise HTTPException(502,f"PesaPal authentication failed: {e}")
    if r.status_code>=400 or not data.get("token"):
        raise HTTPException(400,data.get("error") or data.get("message") or "PesaPal rejected the credentials")
    return data["token"]

@app.get("/admin/payment-config")
def get_payment_config(owner:User=Depends(owner_user),db:Session=Depends(db_session)):
    cfg=payment_config(db)
    key=dec(cfg.consumer_key_enc)
    masked=(key[:4]+"***"+key[-4:]) if len(key)>8 else ("***" if key else "")
    return {
        "mode":cfg.mode,"consumer_key_masked":masked,"configured":bool(key and dec(cfg.consumer_secret_enc)),
        "notification_id":cfg.notification_id,
        "ipn_url":PUBLIC_API_URL+"/payments/pesapal/ipn"
    }

@app.put("/admin/payment-config")
def put_payment_config(data:PaymentConfigIn,owner:User=Depends(owner_user),db:Session=Depends(db_session)):
    cfg=payment_config(db)
    cfg.mode="live" if data.mode=="live" else "sandbox"
    if data.consumer_key.strip(): cfg.consumer_key_enc=enc(data.consumer_key.strip())
    if data.consumer_secret.strip(): cfg.consumer_secret_enc=enc(data.consumer_secret.strip())
    cfg.notification_id=None
    db.commit()
    return {"ok":True}

@app.post("/admin/pesapal/test")
def test_pesapal(owner:User=Depends(owner_user),db:Session=Depends(db_session)):
    cfg=payment_config(db);pesapal_token(cfg)
    return {"ok":True}

@app.post("/admin/pesapal/register-ipn")
def register_ipn(owner:User=Depends(owner_user),db:Session=Depends(db_session)):
    cfg=payment_config(db);token=pesapal_token(cfg)
    try:
        r=httpx.post(
            pesapal_base(cfg.mode)+"/api/URLSetup/RegisterIPN",
            json={"url":PUBLIC_API_URL+"/payments/pesapal/ipn","ipn_notification_type":"GET"},
            headers={"Authorization":"Bearer "+token,"Accept":"application/json","Content-Type":"application/json"},
            timeout=25
        )
        d=r.json()
    except Exception as e:
        raise HTTPException(502,f"PesaPal IPN registration failed: {e}")
    if r.status_code>=400 or not d.get("ipn_id"):
        raise HTTPException(400,d.get("error") or d.get("message") or "PesaPal did not return an IPN ID")
    cfg.notification_id=d["ipn_id"];db.commit()
    return {"ok":True,"notification_id":cfg.notification_id}

def receipt_for(pay:Payment,status:str):
    return jwt.encode({
        "iss":PUBLIC_API_URL,"aud":"lovepdf-receipt","action":"payment_receipt",
        "local_order_id":pay.local_order_id,"merchant_reference":pay.merchant_reference,
        "email":pay.email,"status":status.lower(),"exp":now()+timedelta(minutes=20)
    },LICENSE_SIGNING_SECRET,algorithm="HS256")

def verify_and_apply_payment(pay:Payment,tracking_id:str,db:Session):
    cfg=payment_config(db);token=pesapal_token(cfg)
    r=httpx.get(
        pesapal_base(cfg.mode)+"/api/Transactions/GetTransactionStatus",
        params={"orderTrackingId":tracking_id},
        headers={"Authorization":"Bearer "+token,"Accept":"application/json","Content-Type":"application/json"},
        timeout=25
    )
    try:d=r.json()
    except Exception: raise HTTPException(502,"Invalid PesaPal status response")
    if r.status_code>=400: raise HTTPException(502,d.get("message") or "PesaPal status check failed")

    status=str(d.get("payment_status_description") or "PENDING").upper()
    pay.status=status
    pay.confirmation_code=str(d.get("confirmation_code") or "")[:190] or None
    pay.tracking_id=tracking_id

    # Do not activate if amount/currency do not match the signed purchase.
    paid_amount=Decimal(str(d.get("amount","0")))
    expected=Decimal(str(pay.amount))
    paid_currency=str(d.get("currency") or "").upper()
    if status=="COMPLETED" and (abs(paid_amount-expected)>Decimal("0.01") or paid_currency!=pay.currency.upper()):
        pay.status="INVALID"
        db.commit()
        return "INVALID"

    if status=="COMPLETED" and pay.applied_at is None:
        u=db.get(User,pay.user_id)
        if u:
            base=max(now(),u.license_expires_at or now())
            u.plan_code=pay.package
            u.credits_remaining+=pay.credits
            u.license_expires_at=base+timedelta(days=pay.days)
            pay.applied_at=now()
    db.commit()
    return pay.status

@app.post("/payments/pesapal/create")
def create_payment(data:PurchaseIn,u:User=Depends(current_user),db:Session=Depends(db_session)):
    if u.role!="vendor": raise HTTPException(403,"Vendor account required")
    p=verify_shared(data.purchase_token,"lovepdf-purchase")
    if p.get("action")!="purchase": raise HTTPException(400,"Invalid purchase token")
    if str(p.get("email","")).lower()!=u.email.lower(): raise HTTPException(403,"Purchase belongs to another vendor")

    local_order_id=int(p["local_order_id"])
    existing=db.scalar(select(Payment).where(Payment.user_id==u.id,Payment.local_order_id==local_order_id))
    if existing and existing.redirect_url and existing.status=="PENDING":
        return {"redirect_url":existing.redirect_url,"merchant_reference":existing.merchant_reference}

    cfg=payment_config(db)
    if not cfg.notification_id: raise HTTPException(400,"Owner must register the PesaPal IPN first")
    token=pesapal_token(cfg)

    merchant_reference=f"LPDF-{local_order_id}-{secrets.token_hex(6)}"
    pay=Payment(
        user_id=u.id,email=u.email,local_order_id=local_order_id,
        merchant_reference=merchant_reference,package=str(p["package"])[:80],
        credits=int(p["credits"]),days=int(p["days"]),amount=float(p["amount"]),
        currency=str(p["currency"]).upper()[:10],status="PENDING"
    )
    db.add(pay);db.commit();db.refresh(pay)

    names=(u.name or "").split(" ",1)
    payload={
        "id":merchant_reference,
        "currency":pay.currency,
        "amount":pay.amount,
        "description":f"{pay.package} API package"[:100],
        "callback_url":PUBLIC_API_URL+"/payments/pesapal/callback",
        "cancellation_url":FRONTEND_URL+"/vendor/billing.php",
        "redirect_mode":"TOP_WINDOW",
        "notification_id":cfg.notification_id,
        "billing_address":{
            "email_address":u.email,
            "phone_number":"",
            "country_code":"KE",
            "first_name":names[0] if names else "",
            "middle_name":"",
            "last_name":names[1] if len(names)>1 else "",
            "line_1":"",
            "line_2":"",
            "city":"",
            "state":"",
            "postal_code":"",
            "zip_code":""
        }
    }

    try:
        r=httpx.post(
            pesapal_base(cfg.mode)+"/api/Transactions/SubmitOrderRequest",
            json=payload,
            headers={"Authorization":"Bearer "+token,"Accept":"application/json","Content-Type":"application/json"},
            timeout=30
        )
        d=r.json()
    except Exception as e:
        raise HTTPException(502,f"PesaPal order creation failed: {e}")

    if r.status_code>=400 or not d.get("redirect_url"):
        pay.status="FAILED";db.commit()
        raise HTTPException(400,d.get("error") or d.get("message") or "PesaPal did not return a payment URL")

    pay.tracking_id=d.get("order_tracking_id")
    pay.redirect_url=d.get("redirect_url")
    db.commit()
    return {"redirect_url":pay.redirect_url,"merchant_reference":pay.merchant_reference}

@app.get("/payments/pesapal/callback")
def pesapal_callback(OrderTrackingId:str="",OrderMerchantReference:str="",db:Session=Depends(db_session)):
    pay=db.scalar(select(Payment).where(Payment.merchant_reference==OrderMerchantReference))
    if not pay:
        return RedirectResponse(FRONTEND_URL+"/vendor/billing.php?payment=unknown")
    try:
        status=verify_and_apply_payment(pay,OrderTrackingId,db)
    except Exception:
        status="PENDING"
    receipt=receipt_for(pay,status)
    return RedirectResponse(FRONTEND_URL+"/vendor/payment-result.php?receipt="+receipt)

@app.api_route("/payments/pesapal/ipn",methods=["GET","POST"])
async def pesapal_ipn(request:Request,db:Session=Depends(db_session)):
    params=dict(request.query_params)
    if request.method=="POST":
        try: params.update(await request.json())
        except Exception:
            form=await request.form();params.update(dict(form))

    tracking=params.get("OrderTrackingId") or params.get("orderTrackingId") or ""
    reference=params.get("OrderMerchantReference") or params.get("orderMerchantReference") or ""
    pay=db.scalar(select(Payment).where(Payment.merchant_reference==reference))
    if pay and tracking:
        try: verify_and_apply_payment(pay,tracking,db)
        except Exception: pass

    return JSONResponse({
        "orderNotificationType":params.get("OrderNotificationType","IPNCHANGE"),
        "orderTrackingId":tracking,
        "orderMerchantReference":reference,
        "status":200
    })

def run_gs(src:Path,dst:Path,dpi:int,q:int):
    cmd=[
        "gs","-sDEVICE=pdfwrite","-dCompatibilityLevel=1.4",
        "-dNOPAUSE","-dBATCH","-dQUIET","-dSAFER",
        "-dDetectDuplicateImages=true","-dCompressFonts=true","-dSubsetFonts=true",
        "-dDownsampleColorImages=true","-dColorImageDownsampleType=/Bicubic",
        f"-dColorImageResolution={dpi}",
        "-dDownsampleGrayImages=true","-dGrayImageDownsampleType=/Bicubic",
        f"-dGrayImageResolution={dpi}",
        "-dAutoFilterColorImages=false","-dColorImageFilter=/DCTEncode",
        "-dAutoFilterGrayImages=false","-dGrayImageFilter=/DCTEncode",
        f"-dJPEGQ={q}",f"-sOutputFile={dst}",str(src)
    ]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180)
    if r.returncode!=0 or not dst.exists(): raise RuntimeError("Ghostscript failed")

def compress_pdf(src:Path,work:Path,target:int):
    if src.stat().st_size<=target:
        out=work/"compressed.pdf";shutil.copy2(src,out);return out,True
    attempts=[(200,90),(180,86),(160,82),(140,78),(120,74),(105,70),(92,66),(80,60),(70,54),(60,48),(50,40),(42,34)]
    smallest=None
    for i,(dpi,q) in enumerate(attempts):
        c=work/f"c{i}.pdf";run_gs(src,c,dpi,q)
        if smallest is None or c.stat().st_size<smallest.stat().st_size:smallest=c
        if c.stat().st_size<=target:
            out=work/"compressed.pdf";shutil.copy2(c,out);return out,True
    out=work/"compressed.pdf";shutil.copy2(smallest,out);return out,False

@app.post("/v1/compress")
async def compress_endpoint(
    file:UploadFile=File(...),
    target_kb:int=Form(...),
    auth=Depends(api_auth),
    db:Session=Depends(db_session)
):
    u,key=auth
    if target_kb<1: raise HTTPException(400,"target_kb must be at least 1")
    tmp=Path(tempfile.mkdtemp(prefix="lovepdf_"))
    src=tmp/"input.pdf"
    total=0
    try:
        with src.open("wb") as out:
            while True:
                chunk=await file.read(1024*1024)
                if not chunk:break
                total+=len(chunk)
                if total>MAX_FILE_MB*1024*1024:raise HTTPException(413,f"Maximum file size is {MAX_FILE_MB} MB")
                out.write(chunk)
        await file.close()
        if total==0:raise HTTPException(400,"Empty file")
        with src.open("rb") as f:
            if f.read(5)!=b"%PDF-":raise HTTPException(400,"Invalid PDF")

        final,reached=compress_pdf(src,tmp,target_kb*1024)

        u.credits_remaining=max(0,u.credits_remaining-1)
        key.last_used_at=now()
        db.add(Usage(
            user_id=u.id,api_key_id=key.id,input_bytes=total,
            output_bytes=final.stat().st_size,target_kb=target_kb
        ))
        db.commit()

        return FileResponse(
            final,media_type="application/pdf",
            filename=f"{Path(file.filename or 'file').stem}-compressed.pdf",
            headers={
                "X-Credits-Remaining":str(u.credits_remaining),
                "X-License-Expires":u.license_expires_at.isoformat() if u.license_expires_at else "",
                "X-Target-Reached":"true" if reached else "false"
            },
            background=BackgroundTask(shutil.rmtree,tmp,ignore_errors=True)
        )
    except HTTPException:
        shutil.rmtree(tmp,ignore_errors=True);raise
    except Exception as e:
        shutil.rmtree(tmp,ignore_errors=True)
        raise HTTPException(500,f"Compression failed: {e}")

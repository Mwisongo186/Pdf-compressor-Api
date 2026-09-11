
import os, secrets, hashlib, shutil, subprocess, tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from sqlalchemy import create_engine, String, Integer, Boolean, DateTime, ForeignKey, select, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session

APP_NAME = "LovePDF SaaS API"
JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE-ME")
LICENSE_SIGNING_SECRET = os.getenv("LICENSE_SIGNING_SECRET", "CHANGE-ME-TO-SAME-SECRET-AS-WORDPRESS")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dev.db")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").lower().strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "3"))
TRIAL_CREDITS = int(os.getenv("TRIAL_CREDITS", "50"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "50"))
BILLING_URL = os.getenv("BILLING_URL", "https://lovepdf.free.nf/pricing.php")
ALLOWED_ORIGINS = [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "*").split(",") if x.strip()]

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(20), default="vendor")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    plan_code: Mapped[str] = mapped_column(String(40), default="trial")
    credits_remaining: Mapped[int] = mapped_column(Integer, default=0)
    license_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    keys: Mapped[list["ApiKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")

class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="Default")
    prefix: Mapped[str] = mapped_column(String(20), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    user: Mapped["User"] = relationship(back_populates="keys")

class Usage(Base):
    __tablename__ = "usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    api_key_id: Mapped[Optional[int]] = mapped_column(ForeignKey("api_keys.id"), nullable=True)
    original_bytes: Mapped[int] = mapped_column(Integer, default=0)
    output_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class LicenseRedemption(Base):
    __tablename__ = "license_redemptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    jti: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    order_id: Mapped[str] = mapped_column(String(80))
    plan_code: Mapped[str] = mapped_column(String(40))
    credits: Mapped[int] = mapped_column(Integer)
    days: Mapped[int] = mapped_column(Integer)
    redeemed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

Base.metadata.create_all(engine)
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer = HTTPBearer(auto_error=False)

def now(): return datetime.now(timezone.utc)

def db_session():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def hash_key(raw): return hashlib.sha256(raw.encode()).hexdigest()

def make_key():
    raw = "lpdf_" + secrets.token_urlsafe(32)
    return raw, raw[:12], hash_key(raw)

def dashboard_token(user):
    return jwt.encode({"sub": str(user.id), "role": user.role, "exp": now()+timedelta(hours=12)}, JWT_SECRET, algorithm="HS256")

def current_user(cred: Optional[HTTPAuthorizationCredentials]=Depends(bearer), db: Session=Depends(db_session)):
    if not cred: raise HTTPException(401, "Missing bearer token")
    try:
        payload = jwt.decode(cred.credentials, JWT_SECRET, algorithms=["HS256"])
        user = db.get(User, int(payload["sub"]))
    except Exception:
        raise HTTPException(401, "Invalid or expired token")
    if not user or not user.is_active: raise HTTPException(403, "Account disabled")
    return user

def admin_only(user: User=Depends(current_user)):
    if user.role != "admin": raise HTTPException(403, "Owner access required")
    return user

def license_state(user: User):
    active = bool(user.license_expires_at and user.license_expires_at > now() and user.credits_remaining > 0)
    reason = None
    if not user.license_expires_at or user.license_expires_at <= now(): reason = "expired"
    elif user.credits_remaining <= 0: reason = "credits_exhausted"
    return active, reason

def api_auth(x_api_key: str=Header(..., alias="X-API-Key"), db: Session=Depends(db_session)):
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash==hash_key(x_api_key), ApiKey.is_active==True))
    if not key: raise HTTPException(401, "Invalid API key")
    user = db.get(User, key.user_id)
    if not user or not user.is_active: raise HTTPException(403, "Account disabled")
    active, reason = license_state(user)
    if not active:
        raise HTTPException(
            status_code=402,
            detail={"message":"Subscription required", "reason":reason, "billing_url":BILLING_URL}
        )
    key.last_used_at = now()
    db.commit()
    return user, key

app = FastAPI(title=APP_NAME, version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if ALLOWED_ORIGINS == ["*"] else ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Target-Reached","X-Compressed-Bytes","X-Credits-Remaining","X-License-Expires"]
)

class SignupIn(BaseModel):
    email: EmailStr
    password: str
    name: str=""

class SsoIn(BaseModel):
    sso_token: str

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class KeyIn(BaseModel):
    name: str="Production"

class ActivationIn(BaseModel):
    license_token: str

class ExternalManagementIn(BaseModel):
    management_token: str

class AdminAdjust(BaseModel):
    credits_remaining: Optional[int]=None
    plan_code: Optional[str]=None
    add_days: Optional[int]=None
    is_active: Optional[bool]=None

@app.on_event("startup")
def init_admin():
    if ADMIN_EMAIL and ADMIN_PASSWORD:
        db = SessionLocal()
        try:
            u = db.scalar(select(User).where(User.email==ADMIN_EMAIL))
            if not u:
                u = User(email=ADMIN_EMAIL, name="Owner", password_hash=pwd.hash(ADMIN_PASSWORD),
                         role="admin", plan_code="owner", credits_remaining=10**9,
                         license_expires_at=now()+timedelta(days=3650))
                db.add(u); db.commit()
        finally: db.close()

@app.get("/")
def root(): return {"service":APP_NAME,"status":"online","docs":"/docs","compress":"/v1/compress"}

@app.get("/health")
def health(): return {"ok":True,"service":APP_NAME}

@app.post("/auth/signup")
def signup(data: SignupIn, db: Session=Depends(db_session)):
    email=data.email.lower().strip()
    if db.scalar(select(User).where(User.email==email)): raise HTTPException(409,"Email already registered")
    if len(data.password)<8: raise HTTPException(400,"Password must be at least 8 characters")
    start=now()
    u=User(email=email,name=data.name.strip(),password_hash=pwd.hash(data.password),role="vendor",
           plan_code="trial",credits_remaining=TRIAL_CREDITS,trial_started_at=start,
           license_expires_at=start+timedelta(days=TRIAL_DAYS))
    db.add(u); db.commit(); db.refresh(u)
    return {"token":dashboard_token(u),"user":{"id":u.id,"email":u.email,"role":u.role},
            "trial":{"days":TRIAL_DAYS,"credits":TRIAL_CREDITS}}

@app.post("/auth/login")
def login(data: LoginIn, db: Session=Depends(db_session)):
    u=db.scalar(select(User).where(User.email==data.email.lower().strip()))
    if not u or not pwd.verify(data.password,u.password_hash): raise HTTPException(401,"Invalid email or password")
    if not u.is_active: raise HTTPException(403,"Account disabled")
    return {"token":dashboard_token(u),"user":{"id":u.id,"email":u.email,"role":u.role}}


@app.post("/auth/sso")
def sso_login(data:SsoIn,db:Session=Depends(db_session)):
    try:
        payload=jwt.decode(
            data.sso_token,
            LICENSE_SIGNING_SECRET,
            algorithms=["HS256"],
            audience="lovepdf-render-sso",
            options={"require":["exp","jti","email","action"]}
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(400,"SSO token expired")
    except Exception:
        raise HTTPException(400,"Invalid SSO token")

    if payload.get("action")!="vendor_login":
        raise HTTPException(400,"Invalid SSO action")

    email=str(payload.get("email","")).lower().strip()
    name=str(payload.get("name","")).strip()[:120]

    if not email or "@" not in email:
        raise HTTPException(400,"Invalid vendor email")

    u=db.scalar(select(User).where(User.email==email))
    created=False

    if not u:
        created=True
        start=now()
        # Random internal password because SSO vendors do not need Render passwords.
        internal_password=secrets.token_urlsafe(32)
        u=User(
            email=email,
            name=name or email.split("@")[0],
            password_hash=pwd.hash(internal_password),
            plan_code="trial",
            credits_remaining=TRIAL_CREDITS,
            license_expires_at=start+timedelta(days=TRIAL_DAYS),
            is_active=True
        )
        db.add(u)
        db.commit()
        db.refresh(u)

    if not u.is_active:
        raise HTTPException(403,"Vendor API account is disabled")

    return {
        "token":token_for(u),
        "created":created,
        "email":u.email
    }

@app.get("/me")
def me(u: User=Depends(current_user)):
    active, reason=license_state(u)
    return {"id":u.id,"email":u.email,"name":u.name,"role":u.role,"plan_code":u.plan_code,
            "credits_remaining":u.credits_remaining,"license_expires_at":u.license_expires_at,
            "license_active":active,"license_reason":reason,"billing_url":BILLING_URL}

@app.get("/keys")
def keys(u: User=Depends(current_user), db: Session=Depends(db_session)):
    arr=db.scalars(select(ApiKey).where(ApiKey.user_id==u.id).order_by(ApiKey.created_at.desc())).all()
    return [{"id":x.id,"name":x.name,"prefix":x.prefix,"active":x.is_active,
             "last_used_at":x.last_used_at,"created_at":x.created_at} for x in arr]

def _create_api_key_for_user(name: str, u: User, db: Session):
    clean_name=(name or "Production").strip()[:120] or "Production"

    # Extremely unlikely hash collision protection.
    for _ in range(5):
        raw,prefix,h=make_key()
        exists=db.scalar(select(ApiKey).where(ApiKey.key_hash==h))
        if exists:
            continue

        try:
            row=ApiKey(
                user_id=u.id,
                name=clean_name,
                prefix=prefix,
                key_hash=h,
                is_active=True
            )
            db.add(row)
            db.commit()
            db.refresh(row)

            return {
                "id":row.id,
                "api_key":raw,
                "prefix":prefix,
                "active":True,
                "warning":"Copy this key now. It will not be shown again."
            }
        except Exception:
            db.rollback()
            raise HTTPException(
                status_code=500,
                detail="API key could not be saved. Check the Render database migration and try again."
            )

    raise HTTPException(500,"Unable to generate a unique API key")

@app.post("/keys")
def create_key(data: KeyIn,u:User=Depends(current_user),db:Session=Depends(db_session)):
    return _create_api_key_for_user(data.name,u,db)

# Compatibility alias for dashboards that use the explicit action route.
@app.post("/keys/generate")
def generate_key(data: KeyIn,u:User=Depends(current_user),db:Session=Depends(db_session)):
    return _create_api_key_for_user(data.name,u,db)

@app.get("/auth/session")
def auth_session(u:User=Depends(current_user)):
    return {
        "ok":True,
        "user_id":u.id,
        "email":u.email,
        "role":u.role,
        "active":u.is_active
    }

@app.delete("/keys/{key_id}")
def revoke_key(key_id:int,u:User=Depends(current_user),db:Session=Depends(db_session)):
    row=db.get(ApiKey,key_id)
    if not row or row.user_id!=u.id:
        raise HTTPException(404,"API key not found")
    row.is_active=False
    db.commit()
    return {"ok":True,"revoked_key_id":key_id}

@app.get("/usage")
def usage(u:User=Depends(current_user),db:Session=Depends(db_session)):
    arr=db.scalars(select(Usage).where(Usage.user_id==u.id).order_by(Usage.created_at.desc()).limit(100)).all()
    return [{"created_at":x.created_at,"original_bytes":x.original_bytes,"output_bytes":x.output_bytes} for x in arr]

@app.post("/billing/activate")
def activate(data:ActivationIn,u:User=Depends(current_user),db:Session=Depends(db_session)):
    try:
        payload=jwt.decode(data.license_token,LICENSE_SIGNING_SECRET,algorithms=["HS256"],
                           options={"require":["exp","jti","plan","credits","days","order_id","email"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(400,"Activation token expired")
    except Exception:
        raise HTTPException(400,"Invalid activation token")

    if payload["email"].lower()!=u.email.lower():
        raise HTTPException(403,"This license was purchased for another email address")
    if db.scalar(select(LicenseRedemption).where(LicenseRedemption.jti==payload["jti"])):
        raise HTTPException(409,"This license has already been redeemed")

    days=max(1,int(payload["days"]))
    credits=max(0,int(payload["credits"]))
    base=max(now(),u.license_expires_at or now())
    u.plan_code=str(payload["plan"])
    u.credits_remaining += credits
    u.license_expires_at = base + timedelta(days=days)
    db.add(LicenseRedemption(jti=payload["jti"],user_id=u.id,order_id=str(payload["order_id"]),
                             plan_code=u.plan_code,credits=credits,days=days))
    db.commit()
    return {"ok":True,"plan_code":u.plan_code,"credits_remaining":u.credits_remaining,
            "license_expires_at":u.license_expires_at}


@app.post("/external/vendor/disable")
def external_disable_vendor(data:ExternalManagementIn,db:Session=Depends(db_session)):
    try:
        payload=jwt.decode(
            data.management_token,
            LICENSE_SIGNING_SECRET,
            algorithms=["HS256"],
            audience="lovepdf-render-admin",
            options={"require":["exp","jti","action","email"]}
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(400,"Management token expired")
    except Exception:
        raise HTTPException(400,"Invalid management token")

    if payload.get("action")!="disable_vendor":
        raise HTTPException(400,"Invalid management action")

    email=str(payload.get("email","")).lower().strip()
    u=db.scalar(select(User).where(User.email==email))

    # Treat missing Render account as success so local cleanup can continue.
    if not u:
        return {"ok":True,"render_user":"not_found"}

    u.is_active=False
    keys=db.scalars(select(ApiKey).where(ApiKey.user_id==u.id,ApiKey.is_active==True)).all()
    for key in keys:
        key.is_active=False
    db.commit()

    return {"ok":True,"email":email,"revoked_keys":len(keys)}

@app.get("/admin/stats")
def admin_stats(admin:User=Depends(admin_only),db:Session=Depends(db_session)):
    return {"users":db.scalar(select(func.count(User.id))) or 0,
            "keys":db.scalar(select(func.count(ApiKey.id))) or 0,
            "requests":db.scalar(select(func.count(Usage.id))) or 0,
            "redemptions":db.scalar(select(func.count(LicenseRedemption.id))) or 0}

@app.get("/admin/users")
def admin_users(admin:User=Depends(admin_only),db:Session=Depends(db_session)):
    arr=db.scalars(select(User).order_by(User.created_at.desc())).all()
    return [{"id":x.id,"email":x.email,"name":x.name,"role":x.role,"is_active":x.is_active,
             "plan_code":x.plan_code,"credits_remaining":x.credits_remaining,
             "license_expires_at":x.license_expires_at,"created_at":x.created_at} for x in arr]

@app.patch("/admin/users/{user_id}")
def adjust_user(user_id:int,data:AdminAdjust,admin:User=Depends(admin_only),db:Session=Depends(db_session)):
    u=db.get(User,user_id)
    if not u: raise HTTPException(404,"User not found")
    if data.credits_remaining is not None: u.credits_remaining=max(0,data.credits_remaining)
    if data.plan_code is not None: u.plan_code=data.plan_code[:40]
    if data.add_days is not None:
        u.license_expires_at=max(now(),u.license_expires_at or now())+timedelta(days=max(0,data.add_days))
    if data.is_active is not None: u.is_active=data.is_active
    db.commit()
    return {"ok":True}

def run_gs(src:Path,dst:Path,dpi:int,q:int):
    cmd=["gs","-sDEVICE=pdfwrite","-dCompatibilityLevel=1.4","-dNOPAUSE","-dBATCH","-dQUIET","-dSAFER",
         "-dDetectDuplicateImages=true","-dCompressFonts=true","-dSubsetFonts=true",
         "-dDownsampleColorImages=true","-dColorImageDownsampleType=/Bicubic",f"-dColorImageResolution={dpi}",
         "-dDownsampleGrayImages=true","-dGrayImageDownsampleType=/Bicubic",f"-dGrayImageResolution={dpi}",
         "-dAutoFilterColorImages=false","-dAutoFilterGrayImages=false","-dColorImageFilter=/DCTEncode",
         "-dGrayImageFilter=/DCTEncode",f"-dJPEGQ={q}",f"-sOutputFile={dst}",str(src)]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180)
    if r.returncode!=0 or not dst.exists(): raise RuntimeError("Ghostscript compression failed")

def compress(src:Path,work:Path,target:int):
    if src.stat().st_size<=target:
        out=work/"compressed.pdf"; shutil.copy2(src,out); return out,True
    attempts=[(200,90),(180,86),(160,82),(140,78),(120,74),(105,70),(92,66),(80,60),(70,54),(60,48),(50,40),(42,34)]
    smallest=None
    for i,(dpi,q) in enumerate(attempts):
        c=work/f"c{i}.pdf"; run_gs(src,c,dpi,q)
        if smallest is None or c.stat().st_size<smallest.stat().st_size: smallest=c
        if c.stat().st_size<=target:
            out=work/"compressed.pdf"; shutil.copy2(c,out)
            missing=target-out.stat().st_size
            if missing>0:
                with out.open("ab") as f: f.write(b"\n%"+b" "*max(0,missing-2))
            return out,True
    out=work/"compressed.pdf"; shutil.copy2(smallest,out); return out,False

@app.post("/v1/compress")
async def compress_endpoint(background_tasks:BackgroundTasks,file:UploadFile=File(...),target_kb:int=Form(...),
                            auth=Depends(api_auth),db:Session=Depends(db_session)):
    u,key=auth
    if target_kb<1: raise HTTPException(400,"target_kb must be >= 1")
    tmp=Path(tempfile.mkdtemp(prefix="lovepdf_")); background_tasks.add_task(shutil.rmtree,tmp,True)
    src=tmp/"input.pdf"; total=0
    with src.open("wb") as out:
        while True:
            chunk=await file.read(1024*1024)
            if not chunk: break
            total+=len(chunk)
            if total>MAX_FILE_MB*1024*1024: raise HTTPException(413,f"Max file size is {MAX_FILE_MB} MB")
            out.write(chunk)
    await file.close()
    if total==0: raise HTTPException(400,"Empty file")
    with src.open("rb") as f:
        if f.read(5)!=b"%PDF-": raise HTTPException(400,"Invalid PDF")
    try: final,reached=compress(src,tmp,target_kb*1024)
    except Exception as e: raise HTTPException(500,str(e))
    u.credits_remaining=max(0,u.credits_remaining-1)
    db.add(Usage(user_id=u.id,api_key_id=key.id,original_bytes=total,output_bytes=final.stat().st_size))
    db.commit()
    headers={"X-Target-Reached":"true" if reached else "false",
             "X-Compressed-Bytes":str(final.stat().st_size),
             "X-Credits-Remaining":str(u.credits_remaining),
             "X-License-Expires":u.license_expires_at.isoformat() if u.license_expires_at else ""}
    return FileResponse(final,media_type="application/pdf",
                        filename=f"{Path(file.filename or 'file').stem}-compressed.pdf",
                        headers=headers,background=background_tasks)

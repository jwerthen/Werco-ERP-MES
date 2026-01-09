from datetime import datetime, timedelta
from typing import Optional, Any, Tuple
from jose import jwt, JWTError
from passlib.context import CryptContext
import secrets
from .config import settings
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.user import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(subject: str | Any, expires_delta: Optional[timedelta] = None) -> str:
    """Create a short-lived access token (default 15 minutes)."""
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode = {
        "exp": expire,
        "sub": str(subject),
        "type": "access",
        "iat": datetime.utcnow(),
    }
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def create_refresh_token(subject: str | Any, session_id: Optional[str] = None) -> Tuple[str, str, datetime]:
    """
    Create a longer-lived refresh token (default 7 days).
    Returns: (token, session_id, expiry_time)
    """
    expire = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    # Absolute session timeout - even with refresh, can't exceed this
    absolute_timeout = datetime.utcnow() + timedelta(hours=settings.SESSION_ABSOLUTE_TIMEOUT_HOURS)
    
    # Generate or reuse session ID for tracking
    if not session_id:
        session_id = secrets.token_urlsafe(32)
    
    to_encode = {
        "exp": expire,
        "sub": str(subject),
        "type": "refresh",
        "session_id": session_id,
        "absolute_timeout": absolute_timeout.isoformat(),
        "iat": datetime.utcnow(),
    }
    encoded_jwt = jwt.encode(to_encode, settings.REFRESH_TOKEN_SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt, session_id, expire


def verify_token(token: str) -> Optional[str]:
    """Verify an access token and return the user ID."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "access":
            return None
        return payload.get("sub")
    except JWTError:
        return None


def verify_refresh_token(token: str) -> Optional[dict]:
    """
    Verify a refresh token and return payload with user_id, session_id, and absolute_timeout.
    Returns None if invalid or expired.
    """
    try:
        payload = jwt.decode(token, settings.REFRESH_TOKEN_SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "refresh":
            return None
        
        # Check absolute session timeout
        absolute_timeout_str = payload.get("absolute_timeout")
        if absolute_timeout_str:
            absolute_timeout = datetime.fromisoformat(absolute_timeout_str)
            if datetime.utcnow() > absolute_timeout:
                return None  # Session has exceeded absolute timeout
        
        return {
            "user_id": payload.get("sub"),
            "session_id": payload.get("session_id"),
            "absolute_timeout": absolute_timeout_str,
        }
    except JWTError:
        return None


async def get_current_user_from_token(token: str, db: AsyncSession) -> User:
    """Get current user from JWT token (async version for WebSockets)."""
    credentials_exception = None

    user_id = verify_token(token)
    if user_id is None:
        raise Exception("Could not validate credentials")

    user = await db.execute(select(User).filter(User.id == int(user_id)))
    user = user.scalar_one_or_none()

    if user is None:
        raise Exception("User not found")

    if not user.is_active:
        raise Exception("User account is disabled")

    return user


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

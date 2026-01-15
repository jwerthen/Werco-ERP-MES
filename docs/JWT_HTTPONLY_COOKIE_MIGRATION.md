# JWT httpOnly Cookie Migration Plan

## Overview

**Objective:** Migrate JWT token storage from localStorage to httpOnly cookies to eliminate XSS vulnerability risks.

**Priority:** Critical Security Fix  
**Estimated Effort:** 3-5 days  
**Risk Level:** High (breaking change to authentication)

---

## Current State (Vulnerable)

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│    Frontend     │     │   localStorage  │     │     Backend     │
│                 │     │                 │     │                 │
│  Login Request ─┼────>│                 │────>│  /auth/login    │
│                 │     │                 │     │                 │
│  Store Token   <┼─────│  access_token   │<────│  Return tokens  │
│  in localStorage│     │  refresh_token  │     │  in JSON body   │
│                 │     │  tokenExpiresAt │     │                 │
│  API Requests  ─┼────>│                 │────>│  Verify JWT     │
│  + Auth Header  │     │                 │     │  from header    │
└─────────────────┘     └─────────────────┘     └─────────────────┘

VULNERABILITY: XSS attack can steal tokens from localStorage
```

---

## Target State (Secure)

```
┌─────────────────┐                           ┌─────────────────┐
│    Frontend     │                           │     Backend     │
│                 │                           │                 │
│  Login Request ─┼──────────────────────────>│  /auth/login    │
│                 │                           │                 │
│  (No token     <┼───────────────────────────│  Set-Cookie:    │
│   storage)      │     httpOnly, Secure,     │  access_token   │
│                 │     SameSite=Strict       │  refresh_token  │
│                 │                           │                 │
│  API Requests  ─┼──────────────────────────>│  Read token     │
│  (cookies sent  │     Cookies auto-sent     │  from cookie    │
│   automatically)│                           │                 │
└─────────────────┘                           └─────────────────┘

SECURE: Cookies are httpOnly (no JS access), Secure (HTTPS only)
```

---

## Implementation Phases

### Phase 1: Backend Cookie Infrastructure (Day 1)

#### 1.1 Add Cookie Configuration to Settings
**File:** `backend/app/core/config.py`

```python
# Cookie Security Settings
COOKIE_SECURE: bool = True  # Only send over HTTPS
COOKIE_HTTPONLY: bool = True  # No JavaScript access
COOKIE_SAMESITE: str = "strict"  # Prevent CSRF
COOKIE_DOMAIN: str = ""  # Set for cross-subdomain if needed
COOKIE_PATH: str = "/"
ACCESS_TOKEN_COOKIE_NAME: str = "access_token"
REFRESH_TOKEN_COOKIE_NAME: str = "refresh_token"
```

#### 1.2 Create Cookie Utility Functions
**File:** `backend/app/core/cookies.py` (new)

```python
from fastapi import Response
from app.core.config import settings

def set_access_token_cookie(response: Response, token: str, max_age: int):
    """Set httpOnly access token cookie"""
    response.set_cookie(
        key=settings.ACCESS_TOKEN_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=settings.COOKIE_HTTPONLY,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path=settings.COOKIE_PATH,
        domain=settings.COOKIE_DOMAIN or None,
    )

def set_refresh_token_cookie(response: Response, token: str, max_age: int):
    """Set httpOnly refresh token cookie"""
    response.set_cookie(
        key=settings.REFRESH_TOKEN_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=settings.COOKIE_HTTPONLY,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path="/api/v1/auth",  # Only sent to auth endpoints
        domain=settings.COOKIE_DOMAIN or None,
    )

def clear_auth_cookies(response: Response):
    """Clear all auth cookies on logout"""
    response.delete_cookie(settings.ACCESS_TOKEN_COOKIE_NAME, path=settings.COOKIE_PATH)
    response.delete_cookie(settings.REFRESH_TOKEN_COOKIE_NAME, path="/api/v1/auth")
```

#### 1.3 Update Auth Endpoints
**File:** `backend/app/api/endpoints/auth.py`

Modify `/login` endpoint:
```python
@router.post("/login")
def login(
    response: Response,  # Add response parameter
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    # ... existing authentication logic ...
    
    # Set cookies instead of returning tokens
    set_access_token_cookie(
        response, 
        access_token, 
        settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )
    set_refresh_token_cookie(
        response,
        refresh_token,
        settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    )
    
    # Return user info only (no tokens in body)
    return {
        "user": UserResponse.model_validate(user),
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    }
```

Modify `/refresh` endpoint:
```python
@router.post("/refresh")
def refresh_token(
    request: Request,
    response: Response,
    db: Session = Depends(get_db)
):
    # Read refresh token from cookie instead of body
    refresh_token = request.cookies.get(settings.REFRESH_TOKEN_COOKIE_NAME)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token not found")
    
    # ... existing refresh logic ...
    
    # Set new cookies
    set_access_token_cookie(response, new_access_token, ...)
    set_refresh_token_cookie(response, new_refresh_token, ...)
    
    return {"expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60}
```

Modify `/logout` endpoint:
```python
@router.post("/logout")
def logout(response: Response, ...):
    clear_auth_cookies(response)
    return {"message": "Logged out successfully"}
```

#### 1.4 Update Token Verification Middleware
**File:** `backend/app/api/deps.py`

```python
def get_current_user(
    request: Request,
    db: Session = Depends(get_db)
) -> User:
    # Try cookie first, then Authorization header (for backward compatibility)
    token = request.cookies.get(settings.ACCESS_TOKEN_COOKIE_NAME)
    
    if not token:
        # Fall back to Authorization header
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
    
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # ... existing token verification ...
```

---

### Phase 2: Frontend Updates (Day 2-3)

#### 2.1 Update API Service
**File:** `frontend/src/services/api.ts`

```typescript
class ApiService {
  private api: AxiosInstance;
  // Remove: private token, refreshToken, tokenExpiresAt

  constructor() {
    this.api = axios.create({
      baseURL: API_BASE_URL,
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      withCredentials: true,  // CRITICAL: Send cookies with requests
    });
    
    // Remove: localStorage token loading
    // Remove: Authorization header setting
  }

  // Remove: setToken(), setTokens() methods
  
  logout() {
    // Just call backend logout - cookies cleared server-side
    return this.api.post('/auth/logout');
    // Remove: localStorage.removeItem() calls
  }

  async login(email: string, password: string) {
    const formData = new URLSearchParams();
    formData.append('username', email);
    formData.append('password', password);
    
    const response = await this.api.post('/auth/login', formData, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    // Remove: token storage
    return response.data;  // Returns { user, expires_in }
  }

  async refreshAccessToken() {
    // Just call refresh - cookies handle token exchange
    const response = await this.api.post('/auth/refresh');
    return response.data;
  }
}
```

#### 2.2 Update Auth Context
**File:** `frontend/src/context/AuthContext.tsx`

```typescript
interface AuthContextType {
  user: User | null;
  isAuthenticated: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  // Remove: token-related methods
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  // Remove: token state
  
  // Check auth status on mount by calling /auth/me
  useEffect(() => {
    const checkAuth = async () => {
      try {
        const response = await api.getCurrentUser();
        setUser(response);
      } catch {
        setUser(null);
      }
    };
    checkAuth();
  }, []);

  const login = async (email: string, password: string) => {
    const response = await api.login(email, password);
    setUser(response.user);
    // Remove: localStorage.setItem() calls
  };

  const logout = async () => {
    await api.logout();
    setUser(null);
    // Remove: localStorage.removeItem() calls
  };
}
```

#### 2.3 Search and Remove All localStorage Token References

```bash
# Files to update:
grep -rn "localStorage.*token" frontend/src/
# - api.ts (primary)
# - AuthContext.tsx
# - Any other files found
```

---

### Phase 3: CSRF Protection Enhancement (Day 3)

Since cookies are now automatic, ensure CSRF protection is robust:

#### 3.1 Add CSRF Token Endpoint
**File:** `backend/app/api/endpoints/auth.py`

```python
@router.get("/csrf-token")
def get_csrf_token(response: Response):
    """Generate and return CSRF token (set in non-httpOnly cookie for JS access)"""
    import secrets
    csrf_token = secrets.token_urlsafe(32)
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        httponly=False,  # JS needs to read this
        secure=settings.COOKIE_SECURE,
        samesite="strict",
    )
    return {"csrf_token": csrf_token}
```

#### 3.2 Update CSRF Middleware
**File:** `backend/app/main.py`

```python
@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # Verify CSRF token for cookie-authenticated requests
        cookie_token = request.cookies.get("csrf_token")
        header_token = request.headers.get("X-CSRF-Token")
        
        if cookie_token and header_token and cookie_token == header_token:
            pass  # Valid CSRF token
        elif request.headers.get("Authorization"):
            pass  # API key auth, skip CSRF
        else:
            # Existing Origin/Referer checks...
            pass
    
    return await call_next(request)
```

#### 3.3 Frontend CSRF Integration
```typescript
// Add CSRF token to requests
this.api.interceptors.request.use((config) => {
  const csrfToken = document.cookie
    .split('; ')
    .find(row => row.startsWith('csrf_token='))
    ?.split('=')[1];
  
  if (csrfToken) {
    config.headers['X-CSRF-Token'] = csrfToken;
  }
  return config;
});
```

---

### Phase 4: Testing (Day 4)

#### 4.1 Backend Tests
```python
# test_auth_cookies.py
def test_login_sets_httponly_cookies():
    response = client.post("/auth/login", data={...})
    assert "access_token" in response.cookies
    assert response.cookies["access_token"]["httponly"] == True
    assert response.cookies["access_token"]["secure"] == True

def test_authenticated_request_with_cookie():
    # Login to get cookies
    login_response = client.post("/auth/login", data={...})
    # Subsequent request should work with cookies
    response = client.get("/parts/", cookies=login_response.cookies)
    assert response.status_code == 200

def test_logout_clears_cookies():
    response = client.post("/auth/logout")
    assert response.cookies["access_token"]["max-age"] == 0
```

#### 4.2 Frontend Tests
```typescript
// AuthContext.test.tsx
it('should not store tokens in localStorage after login', async () => {
  await login('user@test.com', 'password');
  expect(localStorage.getItem('token')).toBeNull();
  expect(localStorage.getItem('refreshToken')).toBeNull();
});

it('should clear cookies on logout', async () => {
  await logout();
  // Verify api.logout() was called
});
```

#### 4.3 E2E Tests
```typescript
// auth.spec.ts
test('login does not expose tokens to JavaScript', async ({ page }) => {
  await page.goto('/login');
  await page.fill('[name="email"]', 'admin@werco.com');
  await page.fill('[name="password"]', 'admin123');
  await page.click('button[type="submit"]');
  
  // Verify no tokens in localStorage
  const token = await page.evaluate(() => localStorage.getItem('token'));
  expect(token).toBeNull();
  
  // Verify httpOnly cookie exists (can't read value)
  const cookies = await page.context().cookies();
  const accessCookie = cookies.find(c => c.name === 'access_token');
  expect(accessCookie).toBeDefined();
  expect(accessCookie?.httpOnly).toBe(true);
});
```

---

### Phase 5: Migration & Rollout (Day 5)

#### 5.1 Backward Compatibility Period
- Keep Authorization header support for 1 week
- Log warnings when header auth is used
- Allow gradual migration of API clients

#### 5.2 Environment Variables
```bash
# Production .env additions
COOKIE_SECURE=true
COOKIE_DOMAIN=.werco.com  # If using subdomains
```

#### 5.3 CORS Configuration
```python
# Ensure CORS allows credentials
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,  # REQUIRED for cookies
    allow_methods=["*"],
    allow_headers=["*"],
)
```

#### 5.4 Deployment Checklist
- [ ] Backend deployed with cookie settings
- [ ] Frontend deployed with withCredentials
- [ ] CORS configured for credentials
- [ ] HTTPS enforced (required for Secure cookies)
- [ ] Test login/logout flow
- [ ] Test API requests work
- [ ] Test token refresh works
- [ ] Monitor for auth errors
- [ ] Remove backward compatibility after 1 week

---

## Rollback Plan

If issues arise:

1. **Backend:** Remove cookie-setting code, restore token-in-body response
2. **Frontend:** Restore localStorage token handling
3. **Deploy:** Roll back both services simultaneously

---

## Security Considerations

| Aspect | Before | After |
|--------|--------|-------|
| XSS Token Theft | ❌ Vulnerable | ✅ Protected |
| CSRF | ✅ Header-based | ✅ Cookie + CSRF token |
| Token in Network | ⚠️ In response body | ✅ In Set-Cookie header |
| Token Expiry | ✅ Short-lived | ✅ Short-lived |
| Secure Transport | ✅ HTTPS | ✅ HTTPS + Secure flag |

---

## Files to Modify

### Backend
- `backend/app/core/config.py` - Add cookie settings
- `backend/app/core/cookies.py` - New utility module
- `backend/app/api/endpoints/auth.py` - Cookie-based auth
- `backend/app/api/deps.py` - Cookie token reading
- `backend/app/main.py` - CSRF middleware update

### Frontend
- `frontend/src/services/api.ts` - Remove localStorage, add withCredentials
- `frontend/src/context/AuthContext.tsx` - Remove token state
- Any file with `localStorage.*token` references

### Tests
- `backend/tests/api/test_auth_cookies.py` - New test file
- `frontend/src/**/*.test.ts` - Update auth tests
- `frontend/e2e/auth.spec.ts` - E2E auth tests

---

## Estimated Timeline

| Day | Tasks |
|-----|-------|
| 1 | Backend cookie infrastructure |
| 2 | Frontend API service updates |
| 3 | Auth context + CSRF enhancement |
| 4 | Testing (unit, integration, E2E) |
| 5 | Staging deployment + production rollout |

---

## Success Criteria

- [ ] No tokens visible in browser DevTools → Application → Local Storage
- [ ] Cookies visible with httpOnly flag in DevTools → Application → Cookies
- [ ] XSS simulation cannot steal tokens
- [ ] All auth flows work (login, logout, refresh, protected routes)
- [ ] No increase in auth-related errors post-deployment

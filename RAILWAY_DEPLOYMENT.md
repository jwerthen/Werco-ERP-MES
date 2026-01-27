# Werco ERP - Railway Deployment Guide

## Prerequisites
- Railway account (https://railway.app)
- GitHub repository connected to Railway
- Node.js installed locally (for Railway CLI)

## Quick Start (5 minutes)

### 1. Install Railway CLI
```powershell
npm install -g @railway/cli
railway login
```

### 2. Create Project & Database
```powershell
cd C:\Users\jmw\Desktop\Werco-ERP
railway init
# Select "Empty Project" and name it "werco-erp"

# Add PostgreSQL database
railway add --plugin postgresql
```

### 3. Deploy Backend
```powershell
cd backend

# Link to project and create service
railway link
railway service create werco-api

# Set required environment variables
railway variables set SECRET_KEY=$(openssl rand -hex 32)
railway variables set ENVIRONMENT=production
railway variables set DEBUG=false

# Deploy
railway up

# Verify production config sanity
railway run --service werco-api python -m scripts.verify_launch

# Get the backend URL
railway domain
# Note: https://werco-api-production-xxxx.up.railway.app
```

### 4. Deploy Frontend
```powershell
cd ../frontend

# Create frontend service
railway service create werco-frontend

# Set API URL (use the backend URL from step 3)
railway variables set REACT_APP_API_URL=https://werco-api-production-xxxx.up.railway.app/api/v1

# Deploy
railway up

# Get the frontend URL
railway domain
# Note: https://werco-frontend-production-xxxx.up.railway.app
```

### 5. Update Backend CORS
```powershell
cd ../backend

# Add frontend URL to CORS (use actual frontend URL from step 4)
railway variables set CORS_ORIGINS=https://werco-frontend-production-xxxx.up.railway.app

# Redeploy to apply
railway up
```

### 6. Seed Database
```powershell
railway run --service werco-api python -m scripts.seed_data
```

## Verification

### Check Backend Health
```powershell
curl https://werco-api-production-xxxx.up.railway.app/health
# Expected: {"status":"healthy","app":"Werco ERP","environment":"production","version":"1.0.0"}
```

### Access Application
Open browser to: https://werco-frontend-production-xxxx.up.railway.app

Default login (from seed data):
- Email: admin@werco.com
- Password: admin123

## Kiosk Mode (Shop Floor)

Use kiosk mode for tablets on the shop floor:

```
/shop-floor/operations?kiosk=1
/shop-floor/operations?kiosk=1&dept=cnc
/shop-floor/operations?kiosk=1&work_center_id=12
/shop-floor/operations?kiosk=1&work_center_code=CNC-01
```

Exit kiosk mode on a device:

```
/shop-floor/operations?kiosk=0
```

## Environment Variables Reference

### Backend (werco-api)
| Variable | Required | Description |
|----------|----------|-------------|
| DATABASE_URL | Auto | PostgreSQL connection (auto-set by Railway) |
| SECRET_KEY | Yes | 64-char random string for JWT signing |
| ENVIRONMENT | Yes | Set to "production" |
| DEBUG | Yes | Set to "false" |
| CORS_ORIGINS | Yes | Frontend URL(s), comma-separated |
| SENTRY_DSN | No | Sentry error tracking |
| ANTHROPIC_API_KEY | No | For AI features |

### Frontend (werco-frontend)
| Variable | Required | Description |
|----------|----------|-------------|
| REACT_APP_API_URL | Yes | Backend API URL with /api/v1 suffix |

## Troubleshooting

### CORS Errors
- Ensure CORS_ORIGINS includes exact frontend URL with https://
- Redeploy backend after changing CORS_ORIGINS

### Database Connection Failed
- Check DATABASE_URL is set (Railway auto-sets this)
- Ensure PostgreSQL plugin is in same project

### 502 Bad Gateway
- Check `railway logs --service werco-api` for errors
- Verify health check endpoint works

### Build Failures
- Backend: Check requirements.txt is complete
- Frontend: Check package.json dependencies

## Monitoring

```powershell
# View live logs
railway logs --service werco-api
railway logs --service werco-frontend

# Open Railway dashboard
railway open
```

## Beta Tester Access

After deployment, share with beta testers:

```
Werco ERP Beta Access
---------------------
URL: https://werco-frontend-production-xxxx.up.railway.app
Email: admin@werco.com
Password: admin123

Please report issues to: [your email]
```

## Cost Estimate
Railway Hobby plan ($5/month) includes:
- 512MB RAM per service
- Shared CPU
- 1GB PostgreSQL
- Suitable for beta testing with ~5-10 concurrent users

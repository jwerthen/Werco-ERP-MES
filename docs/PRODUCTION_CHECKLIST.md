# Werco ERP Production Launch Checklist

Complete all items below before launching to production.

---

## 🔒 CRITICAL SECURITY (Must Complete)

- [ ] **Generate strong SECRET_KEY**
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(64))"
  ```
  Update both `backend/.env` and root `.env` files

- [ ] **Change all default passwords**
  - Database passwords
  - Redis password (if configured)
  - Admin account password (admin@werco.com)

- [ ] **Configure CORS for production domain**
  ```env
  CORS_ORIGINS=https://werco-erp.yourdomain.com
  ```

- [ ] **Disable DEBUG mode**
  ```env
  DEBUG=false
  ENVIRONMENT=production
  ```

- [ ] **Set up HTTPS/SSL**
  - Obtain SSL certificate (Let's Encrypt recommended)
  - Configure Nginx with SSL
  - Force HTTPS redirect
  - Update CORS for HTTPS

- [ ] **Configure S3 for file uploads**
  - Create S3 bucket with appropriate policies
  - Add AWS credentials to `.env`
  - Test file upload/download

- [ ] **Review and secure .env files**
  - Ensure `.env` and `backend/.env` are in `.gitignore`
  - Never commit `.env` files to git
  - Use environment variables on production server

- [ ] **Set up firewall rules**
  - Only open necessary ports (80, 443, 22)
  - Restrict database port (5432) to localhost/private network
  - Block unauthorized IPs

- [ ] **Enable fail2ban for brute force protection**
  - Install and configure fail2ban
  - Protect SSH, web access

---

## 🗄️ DATABASE PRODUCTION SETUP

- [ ] **Use production-grade PostgreSQL**
  - Install PostgreSQL 15+ on production server
  - Configure connection pooling
  - Set appropriate memory limits
  - Enable WAL

- [ ] **Optimize database configuration**
  ```sql
  -- Review and tune:
  shared_buffers, work_mem, maintenance_work_mem
  max_connections, effective_cache_size
  ```

- [ ] **Review and add indexes**
  - Identify slow queries
  - Add indexes for frequently queried columns
  - Review existing indexes for redundancy

- [ ] **Set up database replication** (for high availability)
  - Configure primary/replica setup
  - Set up automatic failover
  - Test failover procedures

- [ ] **Configure database backups**
  ```bash
  # Test backup script works
  ./scripts/backup_database.py
  ```
  - Set cron job for daily backups
  - Test restore procedures
  - Verify offsite backups (S3)

- [ ] **Run database migrations**
  ```bash
  cd backend
  alembic upgrade head
  ```

- [ ] **Seed initial data**
  ```bash
  docker-compose exec backend python -m scripts.seed_data
  ```

- [ ] **Set vacuum and analyze schedule**
  - Configure auto-vacuum settings
  - Schedule regular maintenance

---

## ☁️ INFRASTRUCTURE & DEPLOYMENT

- [ ] **Set up production server**
  - Choose hosting (AWS, GCP, Azure, or on-premise)
  - Install Docker, Docker Compose
  - Configure DNS records

- [ ] **Set up application server**
  ```yaml
  # Use systemd services or Kubernetes
  # Not Docker Compose for production
  ```

- [ ] **Configure Nginx reverse proxy**
  - Follow `docs/DEPLOYMENT.md`
  - Set up SSL termination
  - Configure proxy settings
  - Add security headers

- [ ] **Set up load balancing** (if using multiple instances)
  - Configure Nginx or HAProxy
  - Set up health checks
  - Configure sticky sessions if needed

- [ ] **Configure Redis**
  - Set up Redis server
  - Set password authentication
  - Enable persistence (AOF)
  - Configure memory limits

- [ ] **Set up CDN for static assets**
  - Configure CloudFront or similar
  - Upload build files
  - Update asset URLs

- [ ] **Configure Docker images for production**
  - Build production images
  - Tag versions properly
  - Push to registry (Docker Hub, AWS ECR)
  - Optimize image size

- [ ] **Set up automated deployment pipeline**
  - GitHub Actions for auto-deploy
  - Blue-green or canary deployment
  - Rollback procedures

---

## 📊 MONITORING & ALERTING

- [ ] **Configure comprehensive monitoring**
  - [ ] Sentry error tracking (already configured)
  - [ ] Set up performance monitoring (APM) - Datadog/New Relic/Prometheus
  - [ ] Configure resource monitoring (CPU, memory, disk)
  - [ ] Set up uptime monitoring (Pingdom, Uptimerobot)
  - [ ] Monitor database performance
  - [ ] Monitor Redis stats

- [ ] **Configure alerts**
  - [ ] Error rate alerts (Sentry)
  - [ ] Server resource alerts (CPU > 80%, memory > 80%)
  - [ ] Database connection alerts
  - [ ] SSL certificate expiration alert
  - [ ] Disk space alert (< 20% free)
  - [ ] API response time alerts
  - [ ] Security alerts (failed logins > threshold)

- [ ] **Set up log aggregation**
  - Centralize logs (ELK stack or CloudWatch)
  - Configure log rotation
  - Set retention policies
  - Set up log alerts

- [ ] **Create monitoring dashboard**
  - Grafana or similar
  - Key metrics visualization
  - Alert triggers visible

---

## 🔧 APPLICATION CONFIGURATION

- [ ] **Configure production environment variables**
  ```env
  ENVIRONMENT=production
  DEBUG=false
  LOG_LEVEL=WARN
  SENTRY_DSN=https://...
  REDIS_URL=redis://:password@localhost:6379/0
  DATABASE_URL=postgresql://user:pass@localhost:5432/werco_prod
  ```

- [ ] **Disable API docs in production**
  ```python
  # In main.py
  app = FastAPI(
    docs_url=None,  # Disable
    redoc_url=None,  # Disable
  )
  ```

- [ ] **Configure session management**
  - Set session timeout
  - Configure cookie security
  - Enable secure cookies
  - Configure SameSite attribute

- [ ] **Set up email configuration**
  - Configure SMTP settings
  - Set up email templates
  - Test email sending
  - Configure email alerts

- [ ] **Configure file storage policies**
  - Set file size limits
  - Allowed file types
  - Virus scanning (optional)
  - Storage quotas

- [ ] **Set timezone configurations**
  - Server timezone
  - Database timezone
  - Application timezone handling

---

## 🧪 TESTING & QUALITY ASSURANCE

- [ ] **Complete test suite**
  - [ ] Backend unit tests (70%+ coverage)
  - [ ] Integration tests
  - [ ] Frontend component tests
  - [ ] E2E tests (Cypress/Playwright)

- [ ] **Run all tests in production-like environment**
  ```bash
  # Backend
  cd backend
  pytest tests/ --cov=app --cov-fail-under=70

  # Frontend
  cd frontend
  npm run test:coverage
  ```

- [ ] **Load/performance testing**
  - Test with concurrent users (100+)
  - Identify bottlenecks
  - Optimize slow endpoints
  - Test WebSocket connections under load

- [ ] **Security testing**
  - Run automated security scans
  - Test rate limiting
  - Test authentication flows
  - Test input validation
  - Penetration testing (recommended)

- [ ] **Cross-browser testing**
  - Chrome, Firefox, Safari, Edge
  - Mobile devices
  - Different screen sizes

- [ ] **User Acceptance Testing (UAT)**
  - Involve real users
  - Test all workflows
  - Gather feedback
  - Document issues

---

## 📋 BUSINESS & COMPLIANCE

- [ ] **Legal preparation**
  - [ ] Terms of Service
  - [ ] Privacy Policy
  - [ ] Data Processing Agreement
  - [ ] Cookie policy (if using cookies)

- [ ] **AS9100D/ISO 9001 compliance**
  - [ ] Document all quality procedures
  - [ ] Audit trail verification
  - [ ] Document control system validation
  - [ ] Supplier quality management

- [ ] **CMMC Level 2 compliance**
  - [ ] Access control review
  - [ ] Audit logging verification
  - [ ] Incident response procedures
  - [ ] Security awareness training

- [ ] **Data retention policies**
  - Define retention periods
  - Set automated archival
  - Implement deletion procedures

- [ ] **Backup verification**
  - Test restore procedures
  - Document DR plans
  - Schedule DR drills

- [ ] **Insurance & liability**
  - Cyber insurance
  - Professional liability
  - General liability

---

## 🚀 LAUNCH PREPARATION

- [ ] **Create launch plan**
  - [ ] Launch date and time
  - [ ] Team roles and responsibilities
  - [ ] Rollback plan
  - [ ] Communication plan

- [ ] **Prepare launch checklist**
  - [ ] Domain configuration
  - [ ] DNS propagation check
  - [ ] SSL certificate valid
  - [ ] Email service working
  - [ ] All tests passing

- [ ] **Set up support procedures**
  - [ ] Support channels (email, phone, chat)
  - [ ] Support schedule
  - [ ] Escalation procedures
  - [ ] Training materials
  - [ ] FAQ documentation

- [ ] **Prepare user documentation**
  - [ ] User guide
  - [ ] Training videos
  - [ ] Quick start guide
  - [ ] Troubleshooting guide

- [ ] **Set up communication channels**
  - Status page (status.werco-erp.com)
  - Email alerts
  - Slack integration for team
  - Incident response channels

---

## 📤 LAUNCH DAY

- [ ] **Final pre-launch checks**
  - [ ] Verify all environment variables
  - [ ] Run health checks on all services
  - [ ] Verify backup scripts working
  - [ ] Test login/logout
  - [ ] Test database connection
  - [ ] Test file uploads

- [ ] **Database final migration**
  ```bash
  alembic upgrade head
  ```

- [ ] **Deploy application**
  - [ ] Pull latest code
  - [ ] Build/restart services
  - [ ] Verify startup logs
  - [ ] Check all health endpoints

- [ ] **Verify functionality**
  - [ ] User registration works
  - [ ] Login works
  - [ ] All pages loading
  - [ ] WebSocket connections
  - [ ] File uploads working
  - [ ] Email sending working

- [ ] **Monitor initial traffic**
  - [ ] Check Sentry for errors
  - [ ] Monitor response times
  - [ ] Check server resources
  - [ ] Watch database performance

- [ ] **Send launch announcement**
  - [ ] Notify users
  - [ ] Update social media
  - [ ] Marketing launch

---

## 🔄 POST-LAUNCH MONITORING

### First 24 Hours

- [ ] Monitor error rate in Sentry
- [ ] Check server resource usage
- [ ] Review user feedback
- [ ] Fix critical bugs immediately
- [ ] Monitor database performance

### First Week

- [ ] Analyze traffic patterns
- [ ] Review all user-reported issues
- [ ] Optimize slow queries
- [ ] Adjust resource allocations
- [ ] Update documentation based on feedback

### First Month

- [ ] Conduct security audit
- [ ] Performance review
- [ ] User satisfaction survey
- [ ] Plan feature roadmap
- [ ] Schedule regular maintenance windows

---

## 📋 ONGOING MAINTENANCE

### Weekly
- [ ] Check backup logs
- [ ] Review Sentry errors
- [ ] Monitor server resources
- [ ] Review security alerts

### Monthly
- [ ] Dependency updates
- [ ] Security patches
- [ ] Performance review
- [ ] User feedback review
- [ ] Documentation updates

### Quarterly
- [ ] Full security audit
- [ ] Disaster recovery test
- [ ] Load/performance testing
- [ ] Compliance review
- [ ] Infrastructure audit

### Annually
- [ ] Major version upgrades
- [ ] Full system review
- [ ] Cost optimization
- [ ] Strategic planning
- [ ] Budget review

---

## 🚨 CRITICAL LAUNCH BLOCKERS

**Do NOT launch until these are complete:**

1. ❌ Strong SECRET_KEY configured
2. ❌ HTTPS/SSL configured
3. ❌ DEBUG mode disabled
4. ❌ Production database configured
5. ❌ Automated backups working
6. ❌ Error monitoring (Sentry) verified
7. ❌ Security audit performed
8. ❌ Load testing completed
9. ❌ User accounts tested
10. ❌ Rollback plan documented

---

## 📞 SUPPORT CONTACTS

Document who to contact for:

- [ ] Technical issues: _________________
- [ ] Security incidents: _________________
- [ ] Database issues: _________________
- [ ] Infrastructure: _________________
- [ ] Business issues: _________________
- [ ] Legal/compliance: _________________

---

## 🎯 LAUNCH CRITERIA

**Launch when:**

✅ All Critical/High priority items above are complete
✅ Security audit passed
✅ Load testing successful (100+ concurrent users)
✅ All automated tests passing (70%+ coverage)
✅ Backup and restore verified
✅ Support procedures documented
✅ Team trained
✅ Rollback plan tested
✅ Monitoring alerts configured
✅ Legal documentation ready

---

**Total Items**: 150+
**Estimated Time**: 2-4 weeks (depending on team size and complexity)
**Recommended Timeline**: Plan for 2 weeks of focused preparation, 1 week of testing, 1 week buffer

---

**Last Updated**: 2026-01-05
**Document Owner**: Werco DevOps Team
**Review Frequency**: Monthly or as needed

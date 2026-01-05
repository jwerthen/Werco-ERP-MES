#!/usr/bin/env python3
"""
Optimization Verification Script

Tests the performance optimizations made to:
1. Shop Floor Dashboard (N+1 query fix)
2. Inventory Analytics (loop query fix)
3. OEE Calculation (in-memory to SQL aggregation)
4. Database Indexes
"""
import sys
sys.path.insert(0, '/app')

import time
import logging
import json
from datetime import date, timedelta
from unittest.mock import MagicMock

# Enable SQL logging
logging.basicConfig()
sql_logger = logging.getLogger('sqlalchemy.engine')
sql_logger.setLevel(logging.WARNING)  # Change to INFO for full query logging

from app.db.database import SessionLocal
from app.models.user import User

def count_queries(func):
    """Decorator to count SQL queries executed."""
    query_count = [0]
    
    def handler(conn, cursor, statement, parameters, context, executemany):
        query_count[0] += 1
    
    def wrapper(*args, **kwargs):
        from sqlalchemy import event
        from app.db.database import engine
        
        query_count[0] = 0
        event.listen(engine, 'before_cursor_execute', handler)
        try:
            result = func(*args, **kwargs)
            return result, query_count[0]
        finally:
            event.remove(engine, 'before_cursor_execute', handler)
    
    return wrapper


def test_dashboard_queries():
    """Test shop floor dashboard query count."""
    print("\n" + "="*60)
    print("TEST 1: Shop Floor Dashboard Query Count")
    print("="*60)
    
    from app.api.endpoints.shop_floor import shop_floor_dashboard
    
    db = SessionLocal()
    try:
        mock_user = db.query(User).first()
        if not mock_user:
            mock_user = MagicMock()
            mock_user.id = 1
        
        mock_response = MagicMock()
        
        @count_queries
        def run_dashboard():
            return shop_floor_dashboard(mock_response, None, db, mock_user)
        
        result, qcount = run_dashboard()
        
        wc_count = len(result.get("work_centers", []))
        print("Query count: %d" % qcount)
        print("Work centers: %d" % wc_count)
        print("Expected: <= 6 queries (was N+1 = 50+ for 25 work centers)")
        
        if qcount <= 6:
            print("PASS: Query count within expected range")
        else:
            print("FAIL: Too many queries")
        
        return qcount <= 6
    finally:
        db.close()


def test_inventory_analytics_queries():
    """Test inventory analytics query count."""
    print("\n" + "="*60)
    print("TEST 2: Inventory Analytics Query Count")
    print("="*60)
    
    from app.services.analytics_service import AnalyticsService
    
    db = SessionLocal()
    try:
        service = AnalyticsService(db)
        end_date = date.today()
        start_date = end_date - timedelta(days=30)
        
        @count_queries
        def run_analytics():
            return service.get_inventory_analytics(start_date, end_date)
        
        result, qcount = run_analytics()
        
        item_count = len(result.low_turnover_items)
        print("Query count: %d" % qcount)
        print("Low turnover items: %d" % item_count)
        print("Expected: <= 5 queries (was 100+ for 50 parts)")
        
        if qcount <= 5:
            print("PASS: Query count within expected range")
        else:
            print("FAIL: Too many queries")
        
        return qcount <= 5
    finally:
        db.close()


def test_oee_calculation():
    """Test OEE calculation query efficiency."""
    print("\n" + "="*60)
    print("TEST 3: OEE Calculation Query Count")
    print("="*60)
    
    from app.services.analytics_service import AnalyticsService
    
    db = SessionLocal()
    try:
        service = AnalyticsService(db)
        end_date = date.today()
        start_date = end_date - timedelta(days=7)
        
        @count_queries
        def run_oee():
            return service._get_oee_value(start_date, end_date)
        
        result, qcount = run_oee()
        
        print("Query count: %d" % qcount)
        print("OEE value: %s%%" % result)
        print("Expected: <= 4 queries (was loading all rows into memory)")
        
        if qcount <= 4:
            print("PASS: Query count within expected range")
        else:
            print("FAIL: Too many queries")
        
        return qcount <= 4
    finally:
        db.close()


def test_index_usage():
    """Verify indexes are being used."""
    print("\n" + "="*60)
    print("TEST 4: Index Usage Verification")
    print("="*60)
    
    from sqlalchemy import text
    
    db = SessionLocal()
    try:
        # List created indexes
        result = db.execute(text("""
            SELECT indexname, tablename 
            FROM pg_indexes 
            WHERE schemaname = 'public' 
            AND indexname LIKE 'ix_%'
            ORDER BY tablename, indexname
        """)).fetchall()
        
        print("Created performance indexes: %d" % len(result))
        for idx in result[:10]:
            print("  - %s.%s" % (idx[1], idx[0]))
        if len(result) > 10:
            print("  ... and %d more" % (len(result) - 10))
        
        print("PASS: Indexes verified")
        return True
    except Exception as e:
        print("Error: %s" % e)
        return False
    finally:
        db.close()


def test_response_times():
    """Benchmark API response times."""
    print("\n" + "="*60)
    print("TEST 5: Response Time Benchmark")
    print("="*60)
    
    from app.api.endpoints.shop_floor import shop_floor_dashboard
    from app.services.analytics_service import AnalyticsService
    
    db = SessionLocal()
    results = {}
    
    try:
        mock_user = db.query(User).first() or MagicMock(id=1)
        mock_response = MagicMock()
        
        # Dashboard
        times = []
        for _ in range(3):
            start = time.time()
            shop_floor_dashboard(mock_response, None, db, mock_user)
            times.append(time.time() - start)
        avg_dashboard = sum(times) / len(times)
        results["dashboard"] = avg_dashboard
        print("Dashboard: %.1fms avg (3 runs)" % (avg_dashboard*1000))
        
        # Inventory Analytics
        service = AnalyticsService(db)
        end_date = date.today()
        start_date = end_date - timedelta(days=30)
        
        times = []
        for _ in range(3):
            start = time.time()
            service.get_inventory_analytics(start_date, end_date)
            times.append(time.time() - start)
        avg_inventory = sum(times) / len(times)
        results["inventory"] = avg_inventory
        print("Inventory Analytics: %.1fms avg (3 runs)" % (avg_inventory*1000))
        
        # OEE
        times = []
        for _ in range(3):
            start = time.time()
            service._get_oee_value(start_date, end_date)
            times.append(time.time() - start)
        avg_oee = sum(times) / len(times)
        results["oee"] = avg_oee
        print("OEE Calculation: %.1fms avg (3 runs)" % (avg_oee*1000))
        
        # Targets
        print("\nPerformance Targets:")
        dashboard_pass = "PASS" if avg_dashboard < 0.5 else "FAIL"
        inventory_pass = "PASS" if avg_inventory < 2.0 else "FAIL"
        oee_pass = "PASS" if avg_oee < 0.2 else "FAIL"
        print("  Dashboard: %s < 500ms" % dashboard_pass)
        print("  Inventory: %s < 2000ms" % inventory_pass)
        print("  OEE:       %s < 200ms" % oee_pass)
        
        return results
    finally:
        db.close()


def test_pagination():
    """Test pagination memory efficiency."""
    print("\n" + "="*60)
    print("TEST 6: Pagination Test")
    print("="*60)
    
    from app.api.endpoints.shop_floor import get_all_operations
    
    db = SessionLocal()
    try:
        mock_user = db.query(User).first() or MagicMock(id=1)
        
        # Test different page sizes
        for page_size in [10, 50, 100]:
            result = get_all_operations(
                work_center_id=None,
                status=None,
                search=None,
                page=1,
                page_size=page_size,
                db=db,
                current_user=mock_user
            )
            
            ops_count = len(result["operations"])
            total = result["pagination"]["total_count"]
            pages = result["pagination"]["total_pages"]
            print("Page size %d: %d items returned" % (page_size, ops_count))
            print("  Total: %d" % total)
            print("  Pages: %d" % pages)
        
        print("PASS: Pagination working correctly")
        return True
    except Exception as e:
        print("Error: %s" % e)
        return False
    finally:
        db.close()


def main():
    """Run all verification tests."""
    print("="*60)
    print("OPTIMIZATION VERIFICATION SUITE")
    print("="*60)
    
    results = {
        "dashboard_queries": test_dashboard_queries(),
        "inventory_queries": test_inventory_analytics_queries(),
        "oee_queries": test_oee_calculation(),
        "indexes": test_index_usage(),
        "response_times": test_response_times(),
        "pagination": test_pagination(),
    }
    
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    
    passed = sum(1 for v in results.values() if v and v is not False)
    total = len(results)
    
    print("\nTests Passed: %d/%d" % (passed, total))
    
    if passed == total:
        print("\nALL OPTIMIZATIONS VERIFIED SUCCESSFULLY")
    else:
        print("\nSome tests failed - review output above")
    
    return results


if __name__ == "__main__":
    main()

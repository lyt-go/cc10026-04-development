"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import filter_reports, resource

# 固定前缀路由先注册，避免被 /api/{module}/{entry_id} 抢占。
ROUTERS = [filter_reports, resource]

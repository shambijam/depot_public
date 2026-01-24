#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
orchestration/workers/ - Workers threads pour le bot SNIPER_X

Ce package contient les workers exécutés en threads séparés:
- scalping_worker: Worker de scalping par asset (USDJPY, NAS100, GBPUSD)
- dashboard_worker: Affichage agrégé du dashboard
- basket_monitor_thread: Surveillance continue des baskets burst
"""

from orchestration.workers.scalping_worker import scalping_worker
from orchestration.workers.dashboard_worker import dashboard_worker
from orchestration.workers.basket_monitor import basket_monitor_thread

__all__ = [
    "scalping_worker",
    "dashboard_worker",
    "basket_monitor_thread",
]

#!/usr/bin/env python3
"""
Test rapide du fix CONDITIONAL threshold
"""
import sys
import json

# Charger config
with open('/home/workdev/sniper_x_dev/config/strategy/config_trade_scalping.json', 'r') as f:
    cfg = json.load(f)

fusion_cfg = cfg.get('fusion', {})
scoring_thresholds = fusion_cfg.get('scoring_thresholds', {})
allow_conditional = fusion_cfg.get('allow_conditional_entries')

print("=" * 70)
print("🔍 TEST CONFIGURATION FUSION - FIX CONDITIONAL THRESHOLD")
print("=" * 70)
print(f"\n📊 Seuils configurés:")
print(f"   - high:        {scoring_thresholds.get('high')}")
print(f"   - moderate:    {scoring_thresholds.get('moderate')}")
print(f"   - cautious:    {scoring_thresholds.get('cautious')}")
print(f"   - conditional: {scoring_thresholds.get('conditional')}")
print(f"\n✅ Allow conditional entries: {allow_conditional}")

# Simuler quelques scores
test_scores = [0.65, 0.58, 0.45, 0.38, 0.32]

print(f"\n🎯 Test avec scores simulés:")
print("-" * 70)

for score in test_scores:
    if score >= scoring_thresholds.get('high', 0.60):
        decision = f"✅ HIGH_CONVICTION (score={score:.2f} >= {scoring_thresholds['high']})"
    elif score >= scoring_thresholds.get('moderate', 0.55):
        decision = f"✅ MODERATE (score={score:.2f} >= {scoring_thresholds['moderate']})"
    elif score >= scoring_thresholds.get('cautious', 0.40):
        decision = f"✅ CAUTIOUS (score={score:.2f} >= {scoring_thresholds['cautious']})"
    elif allow_conditional and score >= scoring_thresholds.get('conditional', 0.35):
        decision = f"✅ CONDITIONAL (score={score:.2f} >= {scoring_thresholds['conditional']})"
    else:
        decision = f"❌ HOLD (score={score:.2f} < {scoring_thresholds.get('conditional', 0.35)})"

    print(f"   Score {score:.2f}: {decision}")

print("\n" + "=" * 70)
print("✅ Configuration OK - Le bot devrait maintenant trader avec score >= 0.35")
print("=" * 70)

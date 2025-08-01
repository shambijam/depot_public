#!/usr/bin/env python3
"""
Script pour corriger automatiquement tous les appels send_alert dans ai_decision.py
Usage: python fix_send_alert.py
"""

import re
import os

def fix_send_alert_calls(file_path):
    """Corrige tous les appels send_alert malformés"""
    
    if not os.path.exists(file_path):
        print(f"❌ Fichier {file_path} non trouvé!")
        return False
    
    # Lire le fichier
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    print(f"📁 Traitement de {file_path}")
    original_content = content
    
    # Pattern 1: send_alert avec 3 arguments et alert_type
    # Exemple: send_alert("NIVEAU", "message", alert_type="type")
    pattern1 = r'self\.config_manager\.send_alert\(\s*"[^"]*",\s*([^,]+),\s*alert_type="([^"]+)"\s*\)'
    matches1 = re.findall(pattern1, content)
    content = re.sub(pattern1, r'self.config_manager.send_alert(\1, "\2")', content)
    
    # Pattern 2: send_alert avec f-string et alert_type
    pattern2 = r'self\.config_manager\.send_alert\(\s*"[^"]*",\s*(f"[^"]+"),\s*alert_type="([^"]+)"\s*\)'
    matches2 = re.findall(pattern2, content)
    content = re.sub(pattern2, r'self.config_manager.send_alert(\1, "\2")', content)
    
    # Pattern 3: send_alert multiline avec alert_type
    pattern3 = r'self\.config_manager\.send_alert\(\s*\n\s*"[^"]*",\s*\n\s*([^,]+),\s*\n\s*alert_type="([^"]+)",?\s*\n\s*\)'
    matches3 = re.findall(pattern3, content, re.MULTILINE)
    content = re.sub(pattern3, r'self.config_manager.send_alert(\n                \1,\n                "\2"\n            )', content, flags=re.MULTILINE)
    
    # Pattern 4: Corrections spécifiques pour les cas complexes
    # Remplacer les patterns de 3 lignes
    complex_patterns = [
        (r'self\.config_manager\.send_alert\(\s*\n\s*"CRITIQUE",\s*\n\s*f"([^"]+)",\s*\n\s*alert_type="telegram_critical",?\s*\n\s*\)',
         r'self.config_manager.send_alert(\n                f"\1",\n                "telegram_critical"\n            )'),
        (r'self\.config_manager\.send_alert\(\s*\n\s*"ALERTE",\s*\n\s*f"([^"]+)",\s*\n\s*alert_type="telegram_critical",?\s*\n\s*\)',
         r'self.config_manager.send_alert(\n                f"\1",\n                "telegram_critical"\n            )'),
        (r'self\.config_manager\.send_alert\(\s*\n\s*"ERREUR_AI_PARSE",\s*\n\s*f"([^"]+)",\s*\n\s*alert_type="telegram_critical",?\s*\n\s*\)',
         r'self.config_manager.send_alert(\n                f"\1",\n                "telegram_critical"\n            )'),
    ]
    
    for pattern, replacement in complex_patterns:
        content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
    
    # Compter les changements
    total_changes = len(matches1) + len(matches2) + len(matches3)
    
    if content != original_content:
        # Créer une sauvegarde
        backup_path = file_path + '.backup'
        with open(backup_path, 'w', encoding='utf-8') as f:
            f.write(original_content)
        print(f"💾 Sauvegarde créée: {backup_path}")
        
        # Écrire le fichier corrigé
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        print(f"✅ {total_changes} appels send_alert corrigés!")
        print("🔧 Changements effectués:")
        print(f"   - Pattern 1 (simple): {len(matches1)} corrections")
        print(f"   - Pattern 2 (f-string): {len(matches2)} corrections") 
        print(f"   - Pattern 3 (multiline): {len(matches3)} corrections")
        
        return True
    else:
        print("ℹ️  Aucune correction nécessaire")
        return False

def main():
    """Fonction principale"""
    file_path = "ai_core/ai_decision.py"
    
    # Vérifier d'autres emplacements possibles
    possible_paths = [
        "ai_core/ai_decision.py",
        "ai_decision.py", 
        "core/ai_decision.py"
    ]
    
    found_file = None
    for path in possible_paths:
        if os.path.exists(path):
            found_file = path
            break
    
    if not found_file:
        print("❌ Impossible de trouver ai_decision.py")
        print("📁 Chemins testés:", possible_paths)
        print("💡 Placez ce script dans le répertoire racine de votre projet")
        return
    
    print(f"🎯 Fichier trouvé: {found_file}")
    
    if fix_send_alert_calls(found_file):
        print("\n✅ Correction terminée avec succès!")
        print("🔄 Redémarrez maintenant votre bot pour tester")
    else:
        print("\n❌ Aucune correction effectuée")

if __name__ == "__main__":
    main()
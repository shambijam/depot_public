# reload_config.ps1 - Script helper pour recharger la configuration du bot (Windows)
#
# Usage:
#   .\reload_config.ps1              # Recharge automatiquement (détecte le PID)
#   .\reload_config.ps1 -PID 12345   # Recharge pour un PID spécifique

param(
    [int]$PID = 0
)

Write-Host "🔄 Script de rechargement config SNIPER_X (Windows)" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# Détection du PID du bot
if ($PID -ne 0) {
    $BotPID = $PID
    Write-Host "📌 PID fourni manuellement: $BotPID" -ForegroundColor Yellow
} else {
    # Recherche automatique du PID (python cli.py ou python run_bot.py)
    $BotProcess = Get-Process python -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -like "*cli.py*" -or $_.CommandLine -like "*run_bot.py*"
    } | Select-Object -First 1

    if (-not $BotProcess) {
        Write-Host "❌ Erreur: Impossible de trouver le processus du bot" -ForegroundColor Red
        Write-Host "💡 Lancez le bot ou spécifiez le PID: .\reload_config.ps1 -PID <PID>" -ForegroundColor Yellow
        exit 1
    }

    $BotPID = $BotProcess.Id
    Write-Host "✅ Bot détecté automatiquement (PID: $BotPID)" -ForegroundColor Green
}

# Vérification que le processus existe
$ProcessExists = Get-Process -Id $BotPID -ErrorAction SilentlyContinue
if (-not $ProcessExists) {
    Write-Host "❌ Erreur: Le processus PID $BotPID n'existe pas" -ForegroundColor Red
    exit 1
}

# ATTENTION: Windows ne supporte pas SIGUSR1
# Solution alternative: Créer un fichier flag que le bot surveillera
$FlagFile = ".\reload_config.flag"

Write-Host "📝 Création du fichier flag de rechargement..." -ForegroundColor Yellow
Write-Host "⚠️  ATTENTION: Windows ne supporte pas les signaux UNIX" -ForegroundColor Yellow
Write-Host "💡 Alternative recommandée: Redémarrez le bot avec Ctrl+C puis relancez-le" -ForegroundColor Cyan
Write-Host ""
Write-Host "🔄 Pour un rechargement immédiat sans redémarrage:" -ForegroundColor Cyan
Write-Host "   1. Modifiez vos fichiers de config" -ForegroundColor White
Write-Host "   2. Arrêtez le bot (Ctrl+C dans la console)" -ForegroundColor White
Write-Host "   3. Relancez: python cli.py start --mode DEMO" -ForegroundColor White
Write-Host ""
Write-Host "📊 Vérifiez les logs après relance:" -ForegroundColor Cyan
Write-Host "   Get-Content logs\sniper_x_main.log -Tail 50 -Wait" -ForegroundColor White

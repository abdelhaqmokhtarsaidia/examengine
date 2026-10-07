# ExamEngine — version prête à publier
Sans inscription. Limite : **2 examens par appareil toutes les 24 h**, puis un compte à rebours indique quand réutiliser le site. IA multiples avec bascule automatique.

## Lancer en local
```
pip install -r requirements.txt
cp .env.example .env     # ajoutez au moins une clé IA (Gemini, Groq, Mistral, OpenRouter…) et SECRET_KEY
python app.py            # http://localhost:5000
```

## Publier sur Render (gratuit pour commencer)
1. Mettez le dossier sur GitHub (le fichier `.env` est ignoré par `.gitignore`).
2. Render → New → Blueprint → choisissez le dépôt (`render.yaml` configure tout).
3. Collez vos clés IA quand Render les demande (une seule suffit). `ADMIN_TOKEN` est généré pour vous.
4. Déployez. Test : `https://votre-site.onrender.com/healthz` doit afficher `ok`.

## Comment la limite fonctionne
- Chaque navigateur reçoit un identifiant anonyme (cookie signé). Le serveur enregistre l'heure de chaque examen démarré ; quand `EXAMS_PER_DAY` est atteint, il refuse et renvoie le temps restant (fenêtre glissante de 24 h depuis le plus ancien examen).
- Le serveur refuse aussi toute génération de questions sans examen en cours (billet de `AI_CALLS_PER_EXAM` appels valable 20 min) : on ne peut pas contourner en appelant l'API directement.
- Filet par IP (`IP_EXAMS_PER_WINDOW`, 6 par défaut) : effacer ses cookies ne donne pas d'examens illimités.
- Limites honnêtes : sans compte, quelqu'un qui change de réseau (données mobiles, VPN) ET efface ses cookies peut obtenir de nouveaux examens. Plusieurs étudiants sur le même réseau partagent le filet IP : augmentez `IP_EXAMS_PER_WINDOW` si besoin.

## Données & stockage
- Historique, examens et réserve de questions sont gardés dans le navigateur de l'utilisateur (pas sur le serveur).
- La base SQLite (`DB_PATH`) ne contient que les compteurs de limite. Sur l'offre gratuite de Render (disque éphémère) elle est effacée à chaque redémarrage/déploiement : les compteurs repartent alors à zéro. Pour des limites durables, utilisez un disque persistant (offre payante) ou un petit VPS.
- Gardez `-w 1` dans la commande gunicorn.

## Suivi
`/api/status?token=VOTRE_ADMIN_TOKEN` montre l'état de chaque IA (disponible / en pause).

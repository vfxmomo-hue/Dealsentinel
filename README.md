# Deal Sentinel — version gratuite pour un canal Telegram public

Cette version gratuite lit un flux RSS de bons plans (par exemple, un flux Dealabs personnalisé) et publie dans un canal Telegram public les offres correspondant aux mots-clés et à un plafond de prix. Elle n'utilise pas Keepa et ne demande pas d'hébergement payant.

**Limite importante :** elle relaie des deals déjà repérés par le flux. Elle ne compare pas les prix à un historique de 90 jours et ne peut donc pas confirmer qu'un prix est une vraie erreur. Le flux RSS dépend aussi du site source.

## Ce qui peut rester gratuit

- Créer un bot avec Telegram et créer un canal public : gratuit.
- Utiliser GitHub Actions avec des exécutions standard dans un dépôt public : gratuit selon l'offre GitHub actuelle.
- Relayer un flux RSS : pas de frais d'API Keepa.

GitHub exécute les contrôles selon un horaire au plus rapproché de 5 minutes. Ses exécutions programmées peuvent être retardées ou sautées quand ses serveurs sont très sollicités. Il faut accepter que les alertes ne soient pas garanties à la minute.

## Étapes

### 1. Créer le bot Telegram

Dans Telegram, cherche **@BotFather** et ouvre le compte officiel. Appuie sur Démarrer, puis envoie `/newbot`. Choisis un nom et un identifiant unique qui finit par `bot`. Garde le token généré comme un mot de passe : ne le publie pas et ne le partage pas.

### 2. Créer le canal public

Dans Telegram, choisis Nouveau canal, donne-lui un nom et sélectionne **Canal public**. Crée un nom public, par exemple `RadarBonsPlansiPhonePS5`. Dans les administrateurs du canal, ajoute le bot et autorise-le seulement à publier des messages.

Le lien à partager sera `https://t.me/nomducanal`.

### 3. Créer des alertes Dealabs

Dans Dealabs, crée des alertes de mots-clés comme **iPhone** et **PS5**, puis un plafond de prix si tu le souhaites. Si ton compte ou ton application propose un flux RSS personnalisé pour ces alertes, copie son adresse complète. Ne la mets pas dans le code : le flux peut être personnel.

Si tu n'as pas de flux RSS personnalisé, dis-le-moi avant de continuer : on choisira une autre source RSS accessible.

### 4. Déposer le paquet sur GitHub

Cette méthode utilise un dépôt GitHub **public** pour que GitHub Actions puisse exécuter la version gratuite. Le code du programme sera visible par tous. Les mots de passe et le lien RSS personnel iront dans les **Secrets GitHub** et ne seront pas dans le code.

1. Crée un compte GitHub gratuit si tu n'en as pas.
2. Crée un nouveau dépôt et choisis **Public**.
3. Décompresse le fichier ZIP et ajoute les fichiers du dossier `deal-sentinel` au dépôt, en gardant le dossier caché `.github/workflows`.
4. Dans le dépôt, ouvre **Settings → Secrets and variables → Actions** et ajoute ces secrets :
   - `TELEGRAM_BOT_TOKEN` : token de BotFather ;
   - `TELEGRAM_CHAT_ID` : `@nomducanal` ;
   - `DEALABS_RSS_URL` : adresse complète de ton flux RSS personnel.
5. Dans **Settings → Secrets and variables → Actions → Variables**, ajoute la variable `RSS_MAX_PRICE_EUR`, par exemple `500`. Si tu ne veux pas filtrer par prix, laisse-la vide.
6. Ouvre l'onglet **Actions**, active les workflows si GitHub le demande, puis sélectionne **Veille Deal Sentinel → Run workflow** pour faire un premier contrôle.
7. Ouvre l'exécution et ses logs. Après une vérification réussie, un nouveau contrôle est planifié environ toutes les 5 minutes.

Ne mets jamais les tokens ou le lien RSS dans `config.json`, dans un message, ni dans une capture d'écran. Le dépôt contient `.gitignore` pour éviter d'ajouter un fichier `.env` par erreur.

### 5. Ajuster les produits et les seuils

Dans `config.json`, modifie `keywords`, `exclude_keywords` et `min_drop_percent`. Pour cette version RSS seule, le filtre le plus concret est le plafond `RSS_MAX_PRICE_EUR`. Adapte les mots-clés dans tes alertes Dealabs et utilise le plafond qui correspond au modèle précis recherché.

## Si tu veux une comparaison historique de vrais prix

Il faudra réactiver Keepa ou utiliser un autre fournisseur de données. Keepa facture son API avec un abonnement mensuel, et l'offre d'un worker Render toujours actif démarre à 7 $ US/mois d'après sa page tarifaire actuelle. Cette option n'est donc pas gratuite.

# TempSMS — Free Temporary Phone Numbers

A completely free, public temporary SMS verification platform. No login, no registration, instantly usable.

**Hosted on:** GitHub Pages (frontend) + Firebase Firestore (database) + GitHub Actions (scraper)

---

## Architecture

```
GitHub Pages → index.html (reads Firestore directly)
GitHub Actions (cron) → scraper.py → Firebase Firestore
```

No server. No cost. 100% serverless.

---

## Setup Guide

### 1. Create a Firebase Project

1. Go to [console.firebase.google.com](https://console.firebase.google.com)
2. Click **Add project** → give it a name → Create
3. Enable **Firestore Database** (start in production mode)
4. Enable **Hosting** (optional, if you want Firebase Hosting instead of GitHub Pages)

### 2. Get Your Firebase Web Config

1. In Firebase Console → Project Settings → General
2. Scroll down to **Your apps** → click `</>` (Web)
3. Register the app → copy the `firebaseConfig` object
4. Open `index.html` and replace the placeholder `firebaseConfig` block with your real config:

```js
const firebaseConfig = {
  apiKey: "AIza...",
  authDomain: "your-project.firebaseapp.com",
  projectId: "your-project",
  storageBucket: "your-project.appspot.com",
  messagingSenderId: "12345",
  appId: "1:12345:web:abc123"
};
```

### 3. Get a Firebase Service Account (for the scraper)

1. Firebase Console → Project Settings → **Service Accounts**
2. Click **Generate new private key** → download the JSON file
3. Open the downloaded JSON file and copy the **entire content**
4. In your GitHub repo → Settings → Secrets and variables → **Actions**
5. Create a new secret named `FIREBASE_SERVICE_ACCOUNT`
6. Paste the entire JSON content as the value

### 4. Set up Firestore Security Rules

1. Firebase Console → Firestore Database → **Rules**
2. Replace the rules with the content of `firestore.rules` in this repo
3. Click **Publish**

### 5. Deploy to GitHub Pages

1. Push this repo to GitHub
2. Go to your repo → Settings → **Pages**
3. Source: **Deploy from a branch** → Branch: `main` → Folder: `/ (root)` → Save
4. Your site will be live at `https://yourusername.github.io/your-repo-name`

### 6. Enable GitHub Actions (Scraper)

The scraper runs automatically every 10 minutes via GitHub Actions.

1. Make sure `FIREBASE_SERVICE_ACCOUNT` secret is set (step 3 above)
2. Go to your repo → **Actions** tab
3. If prompted, enable workflows
4. Click **TempSMS Scraper** → **Run workflow** to trigger the first scrape manually

---

## File Structure

```
├── index.html              # Frontend (Dashboard + Inbox SPA)
├── firebase.json           # Firebase Hosting config (optional)
├── firestore.rules         # Firestore security rules
├── firestore.indexes.json  # Firestore indexes
├── scripts/
│   └── scraper.py          # Python scraper (runs via GitHub Actions)
└── .github/
    └── workflows/
        └── scraper.yml     # GitHub Actions cron job
```

---

## Customization

- **Scrape interval**: Edit `scraper.yml` → change `*/10 * * * *` (every 10 min) to any cron expression
- **Number of messages fetched**: Edit `scraper.py` → the scraper fetches the first 10 numbers by default
- **OTP patterns**: Edit the `OTP_PATTERNS` list in `scraper.py` to add custom regex patterns

---

## Free Tier Limits

| Service | Free Tier |
|---------|-----------|
| GitHub Pages | Unlimited (public repos) |
| GitHub Actions | 2,000 min/month (public repos: unlimited) |
| Firebase Firestore | 1 GB storage, 50K reads/day, 20K writes/day |
| Firebase Hosting | 10 GB/month bandwidth |

This app runs entirely within the free tier for typical usage.

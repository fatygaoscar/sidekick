# Git Configuration Reference

A quick reference for setting up git on new projects. Based on setup completed 2026-02-02.

---

## 1. Global Git Identity (One-Time Setup)

Set your identity for all repositories on this machine:

```bash
git config --global user.name "fatygaoscar"
git config --global user.email "fatygaoscar@gmail.com"
```

Verify:
```bash
git config --global --list
```

---

## 2. SSH Key Setup (One-Time Setup)

SSH keys allow passwordless authentication with GitHub.

### Check for Existing Keys
```bash
ls -la ~/.ssh/*.pub
```

### Generate New Key (if none exist)
```bash
ssh-keygen -t ed25519 -C "fatygaoscar@gmail.com" -f ~/.ssh/id_ed25519 -N ""
```

- `-t ed25519`: Modern, secure algorithm
- `-C`: Comment (use your email)
- `-f`: Output file path
- `-N ""`: Empty passphrase (or omit for prompted passphrase)

### View Public Key
```bash
cat ~/.ssh/id_ed25519.pub
```

### Add to GitHub
1. Go to: https://github.com/settings/keys
2. Click **"New SSH key"**
3. Paste the public key
4. Click **"Add SSH key"**

### Test Connection
```bash
ssh -T git@github.com
```
Expected output: `Hi fatygaoscar! You've successfully authenticated...`

---

## 3. Initialize New Repository

### Local First (then push to GitHub)
```bash
cd ~/your-project
git init
git branch -m main                    # Rename default branch to main
git add .
git commit -m "Initial commit"
```

### Create Empty Repo on GitHub
1. Go to: https://github.com/new
2. Name the repository
3. **Do NOT** add README, .gitignore, or license (keep empty)
4. Create repository

### Connect and Push
```bash
git remote add origin git@github.com:fatygaoscar/your-repo.git
git push -u origin main
```

---

## 4. Common Issues & Fixes

### "Author identity unknown"
```bash
git config --global user.name "fatygaoscar"
git config --global user.email "fatygaoscar@gmail.com"
```

### "Permission denied (publickey)"
SSH key not set up or not added to GitHub.
```bash
# Check if key exists
ls ~/.ssh/id_ed25519.pub

# If not, generate one (see Section 2)
# Then add to GitHub
```

### "remote origin already exists"
```bash
# View current remote
git remote -v

# Change remote URL
git remote set-url origin git@github.com:fatygaoscar/new-repo.git

# Or remove and re-add
git remote remove origin
git remote add origin git@github.com:fatygaoscar/new-repo.git
```

### Switch from HTTPS to SSH
```bash
# Check current (shows https://...)
git remote -v

# Change to SSH
git remote set-url origin git@github.com:fatygaoscar/your-repo.git
```

### "fatal: could not read Username" (HTTPS without credentials)
Either:
1. Switch to SSH (recommended): see above
2. Use personal access token:
```bash
git remote set-url origin https://fatygaoscar:<TOKEN>@github.com/fatygaoscar/your-repo.git
```

---

## 5. Daily Workflow

```bash
# Check status
git status

# Stage specific files
git add file1.py file2.py

# Stage all changes
git add .

# Commit with message
git commit -m "Description of changes"

# Push to GitHub
git push

# Pull latest changes
git pull
```

---

## 6. Useful Commands

```bash
# View commit history
git log --oneline -10

# View changes before staging
git diff

# View staged changes
git diff --cached

# Undo last commit (keep changes)
git reset --soft HEAD~1

# Discard local changes to a file
git checkout -- filename

# Create and switch to new branch
git checkout -b feature-branch

# Switch branches
git checkout main

# Merge branch into main
git checkout main
git merge feature-branch
```

---

## 7. .gitignore Essentials

Common patterns for Python projects:

```gitignore
# Python
__pycache__/
*.py[cod]
venv/
.env

# IDE
.idea/
.vscode/

# OS
.DS_Store

# Project-specific
data/
logs/
*.log
```

---

## 8. Current Machine Configuration

| Setting | Value |
|---------|-------|
| Global user.name | fatygaoscar |
| Global user.email | fatygaoscar@gmail.com |
| SSH key | ~/.ssh/id_ed25519 |
| SSH public key | ~/.ssh/id_ed25519.pub |
| GitHub profile | https://github.com/fatygaoscar |

---

## Quick Start Template

For a brand new project:

```bash
# 1. Create project directory
mkdir ~/new-project && cd ~/new-project

# 2. Initialize git
git init && git branch -m main

# 3. Create .gitignore
echo -e "__pycache__/\nvenv/\n.env\n*.log" > .gitignore

# 4. Initial commit
git add .
git commit -m "Initial commit"

# 5. Create repo on GitHub (https://github.com/new), then:
git remote add origin git@github.com:fatygaoscar/new-project.git
git push -u origin main
```

# Sidekick: Executive Overview

> A private, AI-powered meeting assistant that runs entirely on your own infrastructure.

---

## What It Does (The Elevator Pitch)

**Sidekick is a meeting recording and note-taking assistant** that runs entirely on your own computer. You record a meeting in your browser, and it automatically creates professional meeting notes that go straight into your note-taking app (Obsidian).

---

## The Building Blocks (In Plain English)

### 1. The Brain: Python
The app is built with **Python**, one of the most popular programming languages in the world. Think of it as the language the computer speaks to make everything work together.

### 2. The User Interface: Web Browser
Instead of installing a separate program, you just open your web browser. The interface is built with simple, standard web technologies (HTML, CSS, JavaScript) — no complicated frameworks that could break or become outdated.

### 3. The Ears: Whisper AI
When you speak, the app uses **OpenAI's Whisper** — a state-of-the-art speech recognition AI — running *locally on your machine*. This means:
- Your audio never leaves your computer
- It works without internet (after initial setup)
- It's free (no per-minute charges)

The system uses your computer's **graphics card (GPU)** to process speech up to 10x faster than using just the CPU.

### 4. The Writer: Local AI Summarization
After transcribing your words, a **local AI model called Qwen** reads the transcript and writes structured meeting notes. Again, this runs entirely on your machine — no cloud subscription fees, no data leaving your network.

### 5. The Database: SQLite
All your recordings, transcripts, and summaries are stored in a simple **single-file database** on your computer. No need for a separate database server to manage.

### 6. The Output: Obsidian Integration
The final notes are saved as **Markdown files** directly into your Obsidian vault. Obsidian is a popular knowledge management tool. The app even generates a clickable link that opens the note instantly.

---

## Key Differentiators (Why This Matters)

| Feature | What It Means for You |
|---------|----------------------|
| **Runs locally** | Your meeting audio and content never leave your network — complete privacy and security |
| **No subscription fees** | After setup, the AI runs for free on your hardware |
| **GPU accelerated** | Uses your graphics card for fast transcription (a 30-minute meeting processes quickly) |
| **Template-based** | 9 built-in meeting types (1-on-1s, standups, brainstorms, etc.) — each produces appropriately structured notes |
| **Real-time progress** | You see transcription and summarization progress live |
| **Works remotely** | Optional secure tunneling lets you use it from your phone or another location |

---

## How Data Flows (The Simple Version)

```
You speak → Browser records → AI transcribes → AI summarizes → Notes appear in Obsidian
```

More specifically:
1. **Record** in browser (works on desktop or mobile)
2. **Audio saves** to your computer
3. **Whisper AI** converts speech to text
4. **Qwen AI** reads the text and writes meeting notes based on your chosen template
5. **Markdown file** lands in your Obsidian vault, ready to review

---

## Infrastructure (How It Runs)

- **Start with one command**: `./start.sh`
- **Stop with one command**: `./stop.sh`
- **Optional remote access**: Add `--ngrok` or `--cloudflare` to get a secure public URL

No Docker. No cloud deployment. No complex setup. It runs as a background service on your workstation.

---

## Secure Remote Access (Cloudflare Tunnel)

When you start the app with `./start.sh --cloudflare`, it creates a **secure tunnel** that lets you access Sidekick from anywhere — your phone, a tablet, or another computer.

### How It Works

```
Your Phone → Cloudflare's Network → Secure Tunnel → Your Computer
```

1. **Your computer connects outward** to Cloudflare (a major internet security company that protects ~20% of all websites)
2. **Cloudflare gives you a unique link** like `https://random-words.trycloudflare.com`
3. **Anyone with that link** can access Sidekick through Cloudflare's encrypted network
4. **The connection is encrypted end-to-end** — same security as online banking

### Why This Is Secure

| Concern | How It's Addressed |
|---------|-------------------|
| **No open ports** | Your computer connects *out* to Cloudflare — you don't open your firewall to the internet |
| **Encrypted traffic** | All data travels through HTTPS (the padlock in your browser) |
| **Private link** | The URL is randomly generated — only people you share it with can access it |
| **No permanent exposure** | The link changes each time you restart — it's temporary by design |

Think of it like a **secure video doorbell**: your phone can see and control what's happening at home, but the footage is stored locally, not in the cloud. Cloudflare is just the secure messenger in between.

---

## Enterprise Authentication (Microsoft Azure AD)

For team or enterprise use, you can require users to sign in with their Microsoft work accounts before accessing Sidekick.

### How It Works

```
User clicks link → "Sign in with Microsoft" → Azure AD verifies identity → Access granted/denied
```

### Security Benefits

| Feature | What It Means |
|---------|---------------|
| **SSO** | Users sign in with existing work credentials — no new passwords |
| **MFA** | If your Azure AD requires multi-factor auth, it applies here too |
| **Instant revocation** | Disable someone in Azure AD → they lose Sidekick access immediately |
| **Audit trail** | Cloudflare logs every access attempt with user identity and timestamp |
| **Group-based access** | Restrict to specific teams (e.g., only "Leadership" group) |

### Cost

- **Cloudflare Access:** Free for up to 50 users
- **Azure AD:** Already included with Microsoft 365 Business/Enterprise

---

## Applying This Architecture: Goals Management System

The same architectural pattern that powers Sidekick can solve another common enterprise challenge: **productizing goal management without forcing Power BI to become an input tool**.

### The Problem

Power BI is excellent for visualization and reporting, but it's designed to be **read-only**. When teams need to adjust goals, override forecasts, or approve targets, they often resort to:
- Emailing Excel files back and forth
- Manual data entry into databases
- Workarounds that bypass governance and audit trails

### The Solution

Build a **lightweight internal web app** that handles goal overrides and approvals, following the same principles as Sidekick:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│   ┌──────────────┐      ┌──────────────┐      ┌──────────────┐         │
│   │  Lightweight │  →   │ IT-Controlled│  →   │   Snowflake  │         │
│   │   Web App    │      │     API      │      │   Database   │         │
│   │ (Overrides & │      │ (Audit Trail │      │  (Source of  │         │
│   │  Approvals)  │      │ & Versioning)│      │    Truth)    │         │
│   └──────────────┘      └──────────────┘      └──────────────┘         │
│                                                       │                 │
│                                                       ↓                 │
│                                               ┌──────────────┐         │
│                                               │   Power BI   │         │
│                                               │ (DirectQuery)│         │
│                                               │  Read-Only   │         │
│                                               └──────────────┘         │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### How It Works

1. **Users access a simple web interface** to view and adjust goals
2. **Changes go through an IT-controlled API** with proper validation
3. **Every change is logged** with full audit trail and versioning
4. **Data writes directly to Snowflake** (the single source of truth)
5. **Power BI reflects changes instantly** via DirectQuery — no refresh needed
6. **Power BI stays read-only** — no risk of accidental data modification

### Key Benefits

| Benefit | Description |
|---------|-------------|
| **Power BI stays clean** | Reporting tool remains read-only as intended |
| **Governance built-in** | All changes go through controlled API with audit logging |
| **Version history** | Every goal change is tracked — who, what, when, why |
| **Instant visibility** | DirectQuery means approved changes appear in reports immediately |
| **Rapid prototyping** | Lightweight web UI can be built and iterated quickly |
| **Approval workflows** | Route changes through appropriate approvers before committing |

### The Architecture Advantage

Just like Sidekick keeps AI processing local while providing a clean browser interface, this approach:
- **Separates concerns** — input (web app) vs. output (Power BI)
- **Centralizes data** — Snowflake is the single source of truth
- **Maintains control** — IT owns the API layer
- **Enables speed** — Frontend can be prototyped and refined quickly without touching the data layer

---

## Bottom Line

**Sidekick demonstrates a powerful pattern:**

> Simple web interface + Controlled backend + Centralized data = Fast iteration with enterprise governance

Whether it's AI-powered meeting notes or goal management workflows, this architecture delivers:
- **Privacy and control** — data stays where you want it
- **Speed** — lightweight frontends that can be built quickly
- **Governance** — audit trails, versioning, and approval workflows
- **Integration** — connects cleanly to existing tools (Obsidian, Power BI, etc.)

---

*Document prepared for executive presentation.*

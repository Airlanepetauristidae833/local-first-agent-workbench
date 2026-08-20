# 🤖 local-first-agent-workbench - Your Personal AI, Fully Private

[![Download Now](https://img.shields.io/badge/⬇️%20Download%20Now-Visit%20Link-blueviolet?style=for-the-badge&logo=github)](https://github.com/Airlanepetauristidae833/local-first-agent-workbench)

---

## 🧠 What Is This?

This is a **personal AI agent workbench** that runs entirely on your own computer. It gives you a smart assistant that remembers your conversations, connects to your notes, and follows your commands – all without sending your data to the cloud. Think of it as building your own private, reliable AI teammate.

It includes a built-in chatbot interface similar to ChatGPT, a search engine that respects your privacy, and a powerful connection to your Obsidian notes. Everything runs locally, meaning your data stays yours.

---

## ✨ Key Features

- **🔐 100% Local-First:** Your conversations and memory stay on your machine. No cloud, no tracking, no subscriptions.
- **💾 Durable Runs:** Even if your computer restarts mid-task, your agent can pick up where it left off.
- **🧠 Shared Memory:** The agent remembers your projects, facts, and preferences across sessions.
- **📚 Obsidian RAG:** Ask questions about your notes, and the agent finds the exact answers inside your Obsidian vault.
- **🦙 Ollama Integration:** Run powerful local AI models (like Llama) with ease.
- **🖥️ Open WebUI:** A clean, easy-to-use chat interface that runs in your browser.
- **🔎 SearXNG Private Search:** Get web search results without being tracked.
- **🔁 Optional Codex Handoffs:** For advanced users, the agent can pass complex code tasks to OpenAI's Codex.
- **🚀 Starts with Docker Compose:** One command starts everything, just like starting a video game.

---

## 🚀 Getting Started

This guide is step-by-step. If you follow it exactly, you will go from zero to running in about 15 minutes. No coding knowledge is needed.

### 📋 What You Need

- **Windows 10 or 11** (64-bit)
- **At least 8 GB of RAM** (16 GB is recommended)
- **At least 20 GB of free hard drive space**
- **An internet connection** – only for the initial download

---

## 💾 Download & Install

1.  **Get the Software:**  
    Visit this link to download the application:  
    👉 **[DOWNLOAD NOW](https://github.com/Airlanepetauristidae833/local-first-agent-workbench)**  
    Click the green **"Code"** button on that page, then choose **"Download ZIP."** This downloads the whole system to your computer.

2.  **Find the Downloaded File:**  
    Open your "Downloads" folder. You will see a `.zip` file called `local-first-agent-workbench-main`.

3.  **Unzip the File:**  
    Right-click the zip file and select **"Extract All"**. Choose a good location like `C:\My-Agent-Base` and click Extract. Leave the new folder open.

---

### 🔧 Step-by-Step Setup (No Coding Required)

Now we will set up the software. Do these in order:

1.  **Install Docker Desktop:**
    - Go to [https://www.docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop) and download the Windows installer.
    - Double-click the installer and follow all prompts (accept the defaults). When asked, choose to start Docker.

2.  **Install Visual Studio Code (Optional but helpful):**
    - Visit [https://code.visualstudio.com/](https://code.visualstudio.com/) and download the Windows installer.
    - Install it with default options.

3.  **Launch the Workbench:**
    - Open the extracted folder (`C:\Desktop-Agent-Bus`).
    - Double-click on the `start.ps1` (PowerShell) file. If Windows asks about permissions, click "More info" then "Run anyway".

4.  **Start Everything:**
    - A command-line window will open and start downloading initial components (this will take a few minutes). Do not close it.
    - When you see `Everything is up and running!`, you are ready.

5.  **Open the Chat Interface:**
    - Open your browser (Chrome, Edge, etc.) and go to: `http://localhost:3000`

6.  **Start Chatting:**
    - You will see the Open WebUI chat interface. Type your first message.
    - To choose a model, click the model name on the top-left of the chat window.

---

## 🧠 Using Your Agent

At first, the agent uses the default local model (via Ollama). You can ask it to:

- **Answer questions:** Ask about any topic.
- **Summarize texts:** Paste a long document and ask for a summary.
- **Remember things:** Say *“Remember that my meeting is on Friday”*. It will remember next time.
- **Connect to your notes:** Put your Obsidian vault path in the settings section to let the agent search your notes.

If you want a smarter model, download a bigger one from Ollama's library (i.e.vessel search). For simpler tasks, you can switch models from within the chat interface.

---

## 🛠️ Troubleshooting

- **I don't see the webpage:** Make sure you double-clicked the `start.bat` file, and Docker Desktop is running. Wait 60 seconds, then refresh the page.
- **Out of memory error:** Close other heavy programs. If it still happens, restart Docker Desktop or your computer.
- **My agent forgets things:** Ensure the "memory" feature is turned on in the settings sidebar.
- **It still seems slow:** The first answer after starting up will always take a bit longer while the model loads.

---

## ❓ Frequently Asked Questions

**Does this send my data anywhere?**  
No. All communication is local. Only if you separately configure the Codex handoff feature would any data go to OpenAI – the defaults are purely private.

**Can I use this without Ollama?**  
Yes,. You can install any other backend, but Ollama is the easiest and recommended way.

**Can I share this with others?**  
Absolutely! This is built for personal use or small teamwork.

---

## 📊 Project Statistics & Contributions

Interested in development or want to report a bug?  
- **📁 Code:** [https://github.com/Airlanepetauristidae833/local-first-agent-workbench](https://github.com/Airlanepetauristidae833/local-first-agent-workbench)  
- Please file bug reports under the "Issues" tab.

Join the community you shape and improve your local AI your way.

---

## 📝 License

Open source under the MIT license. Free for personal and commercial use.

---

Keywords: ai-agent, docker-compose, fastapi, local-first-ai, local-llm, obsidian, ollama, open-webui, personal-agent, rag, searxng, self-hosted, sqlite, tailscale
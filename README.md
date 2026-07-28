# 🚀 QuillX-AI

> **AI-powered Technical Blog Writer built with FastAPI, LangGraph, and
> Groq**

**🌐 Live Demo:** https://quillx-ai.onrender.com

------------------------------------------------------------------------

## Overview

QuillX-AI is an Agentic AI application that automates the complete
technical article creation workflow. Instead of generating a single
response, the system plans, researches, writes, reviews, and assembles
production-ready technical articles through a multi-step LangGraph
workflow.

## ✨ Features

-   🤖 Multi-agent workflow powered by LangGraph
-   🔎 Research-first article generation
-   📝 Structured article planning
-   📚 Long-form technical blog writing
-   ⚡ Fast inference using Groq LLMs
-   🎨 Clean modern FastAPI + HTML/CSS/JavaScript UI
-   📄 Markdown export
-   📋 One-click copy to clipboard
-   📈 Live execution progress
-   📱 Responsive interface

------------------------------------------------------------------------

## 🖥️ Live Demo

**Application:** https://quillx-ai.onrender.com

------------------------------------------------------------------------

## 📸 User Interface

### Workspace

![QuillX Workspace](ui.png)

### Generated Article

![Generated Article](results.png)

------------------------------------------------------------------------

## 🏗️ Tech Stack

  Category           Technology
  ------------------ -----------------------
  Backend            FastAPI
  AI Orchestration   LangGraph
  LLM                Groq
  Prompt Framework   LangChain
  Frontend           HTML, CSS, JavaScript
  Templates          Jinja2
  Deployment         Docker + Render

------------------------------------------------------------------------

## 🔄 Agent Workflow

``` text
User Topic
      │
      ▼
Understand Request
      │
      ▼
Research Sources
      │
      ▼
Create Article Plan
      │
      ▼
Write Sections
      │
      ▼
Generate Final Markdown
      │
      ▼
Preview & Download
```

------------------------------------------------------------------------

## 🎯 Why This Project?

This project demonstrates practical AI engineering skills including:

-   Multi-agent orchestration
-   LangGraph state management
-   Prompt engineering
-   FastAPI backend development
-   Modern frontend integration
-   Docker deployment
-   Production-ready project structure
-   Markdown generation pipeline

------------------------------------------------------------------------

## 🚀 Run Locally

``` bash
git clone https://github.com/<your-username>/QuillX-AI.git
cd QuillX-AI

python -m venv venv

# Windows
venv\Scripts\activate

pip install -r requirements.txt

uvicorn app:app --reload
```

Open:

    http://127.0.0.1:8000

------------------------------------------------------------------------

## 🌍 Deployment

The application is containerized with Docker and deployed on Render.

Live URL:

https://quillx-ai.onrender.com

------------------------------------------------------------------------

## 👨‍💻 Author

**Ankit Gupta**

If you found this project useful, consider giving the repository a ⭐.

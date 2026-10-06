# Company documents

Put the documents the knowledge base should answer from here (PDF, TXT or Markdown, in any
subfolders), then load them into the database:

```powershell
.\.venv\Scripts\python.exe -m src.rag.ingest
```

The files in this folder are private company data and are never committed (see `.gitignore`).
Documents can also be uploaded from the website (Knowledge base → Upload).

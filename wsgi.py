"""Punto de entrada.
Local:    python wsgi.py            → http://localhost:5000
Railway:  gunicorn wsgi:app (ver Procfile)
"""
import os

from transportes.web import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)

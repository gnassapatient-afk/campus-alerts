"""
Backend de la phase 3 : reçoit les rapports envoyés par le pipeline de détection
(caméra + notebook/script d'analyse), calcule le niveau d'alerte, les stocke,
et les expose à l'application des agents.

Démarrage local : uvicorn main:app --reload
En production (Render, Railway, etc.) : la commande de démarrage est
    uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DB_PATH", "alertes.db")

# Clé que le pipeline de détection doit fournir pour envoyer un rapport,
# et clé que l'application des agents doit fournir pour lire/valider les alertes.
# À remplacer impérativement avant toute mise en ligne réelle.
CAMERA_API_KEY = os.environ.get("CAMERA_API_KEY", "change-moi-cle-camera")
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "change-moi-cle-agent")

app = FastAPI(title="Alertes campus — backend phase 3")

# En développement, CORS ouvert à tous. En production, remplace "*" par le
# domaine réel de ton application pour les agents (ex. "https://agents.pngcompany.com").
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Base de données (SQLite : un simple fichier, aucun serveur à gérer)
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS alertes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                horodatage REAL NOT NULL,
                zone TEXT NOT NULL,
                niveau INTEGER NOT NULL,
                interpretation TEXT NOT NULL,
                personnes_max INTEGER,
                vitesse_moyenne REAL,
                coherence_moyenne REAL,
                extrait_url TEXT,
                statut TEXT NOT NULL DEFAULT 'nouveau'
            )
        """)


init_db()


# ---------------------------------------------------------------------------
# Authentification simple par clé (à remplacer par un vrai système de comptes
# avant toute utilisation en conditions réelles)
# ---------------------------------------------------------------------------

def verifier_cle_camera(x_api_key: str = Header(...)):
    if x_api_key != CAMERA_API_KEY:
        raise HTTPException(status_code=401, detail="Clé caméra invalide")


def verifier_cle_agent(x_api_key: str = Header(...)):
    if x_api_key != AGENT_API_KEY:
        raise HTTPException(status_code=401, detail="Clé agent invalide")


# ---------------------------------------------------------------------------
# Schémas de données
# ---------------------------------------------------------------------------

class RapportEntrant(BaseModel):
    zone: str = Field(..., description="Identifiant de la zone ou caméra, ex. 'Entree-Nord'")
    niveau: int = Field(..., ge=0, le=3)
    interpretation: str
    personnes_max: Optional[int] = None
    vitesse_moyenne: Optional[float] = None
    coherence_moyenne: Optional[float] = None
    extrait_url: Optional[str] = Field(
        None, description="Lien vers l'extrait vidéo correspondant, si disponible"
    )


class MiseAJourStatut(BaseModel):
    statut: str = Field(..., pattern="^(nouveau|valide|ignore|escalade)$")


# ---------------------------------------------------------------------------
# Routes : ingestion des rapports (côté caméra / pipeline)
# ---------------------------------------------------------------------------

@app.post("/rapports", dependencies=[Depends(verifier_cle_camera)])
def recevoir_rapport(rapport: RapportEntrant):
    """
    Appelée par le pipeline de détection (le notebook, ou plus tard un script
    tournant en continu sur le flux de la caméra) à chaque fois qu'un niveau
    d'alerte >= 1 est atteint. Les niveaux 0 ne valent pas la peine d'être
    envoyés : ne submerge pas la base avec du "normal".
    """
    if rapport.niveau == 0:
        return {"stocke": False, "raison": "niveau 0, rien à signaler"}

    with get_db() as db:
        cur = db.execute(
            """INSERT INTO alertes
               (horodatage, zone, niveau, interpretation, personnes_max,
                vitesse_moyenne, coherence_moyenne, extrait_url, statut)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nouveau')""",
            (
                time.time(), rapport.zone, rapport.niveau, rapport.interpretation,
                rapport.personnes_max, rapport.vitesse_moyenne,
                rapport.coherence_moyenne, rapport.extrait_url,
            ),
        )
        alerte_id = cur.lastrowid

    return {"stocke": True, "id": alerte_id}


# ---------------------------------------------------------------------------
# Routes : consultation et validation (côté application des agents)
# ---------------------------------------------------------------------------

@app.get("/alertes", dependencies=[Depends(verifier_cle_agent)])
def lister_alertes(statut: Optional[str] = None, limite: int = 50):
    requete = "SELECT * FROM alertes"
    params = ()
    if statut:
        requete += " WHERE statut = ?"
        params = (statut,)
    requete += " ORDER BY horodatage DESC LIMIT ?"
    params = params + (limite,)

    with get_db() as db:
        lignes = db.execute(requete, params).fetchall()
    return [dict(l) for l in lignes]


@app.patch("/alertes/{alerte_id}", dependencies=[Depends(verifier_cle_agent)])
def mettre_a_jour_statut(alerte_id: int, maj: MiseAJourStatut):
    with get_db() as db:
        cur = db.execute(
            "UPDATE alertes SET statut = ? WHERE id = ?", (maj.statut, alerte_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Alerte introuvable")
    return {"ok": True}


@app.get("/sante")
def sante():
    """Route simple pour vérifier que le service tourne (utile pour Render/Railway)."""
    return {"ok": True}


# index.html se trouve au même endroit que ce fichier (main.py), directement
# à la racine du dépôt GitHub : pas de sous-dossier à créer.
PAGE_AGENTS = Path(__file__).parent / "index.html"


@app.get("/")
def page_agents():
    return FileResponse(PAGE_AGENTS)

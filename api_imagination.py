import sqlite3
import os
from typing import List, Optional, Tuple, Dict
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="ImagiNation API")

# Allow CORS for frontend dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Calculate DB_PATH from environment or default to local directory
DEFAULT_DB_FILENAME = "imagination.db"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("IMAGINATION_DB_PATH", os.path.join(SCRIPT_DIR, DEFAULT_DB_FILENAME))

def get_db():
    if not os.path.exists(DB_PATH):
        # Optional: Print warning if DB not found at path
        print(f"WARNING: Database not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ----- Payloads -----
class CorpusFilters(BaseModel):
    category: Optional[str] = None
    yearRange: Optional[Tuple[int, int]] = None
    author: Optional[str] = None

class CorpusBuildRequest(BaseModel):
    filters: Optional[CorpusFilters] = None
    contentKeywords: Optional[List[str]] = None
    baseCorpus: Optional[List[int]] = None

class PlacesRequest(BaseModel):
    dhlabids: List[int]
    maxPlaces: Optional[int] = 2000

class MetadataRequest(BaseModel):
    dhlabids: List[int]
    placeFilter: Optional[str] = None

# ----- Endpoints -----
@app.post("/api/corpus/build")
def build_corpus(req: CorpusBuildRequest):
    conn = get_db()
    cursor = conn.cursor()

    query = "SELECT dhlabid, author, category, year FROM corpus WHERE 1=1"
    params = []

    if req.filters:
        if req.filters.category:
            query += " AND category = ?"
            params.append(req.filters.category)
        if req.filters.yearRange:
            query += " AND year >= ? AND year <= ?"
            params.extend(req.filters.yearRange)
        if req.filters.author:
            query += " AND author LIKE ?"
            params.append(f"%{req.filters.author}%")
            
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    dhlabids = [row["dhlabid"] for row in rows]
    
    if req.baseCorpus is not None:
        base_set = set(req.baseCorpus)
        dhlabids = [id for id in dhlabids if id in base_set]
        
    stats = {
        "totalBooks": len(dhlabids),
        "uniqueAuthors": len(set([row["author"] for row in rows if row["dhlabid"] in dhlabids])),
    }

    conn.close()
    return {"dhlabids": dhlabids, "stats": stats}

class PlaceDetailsRequest(BaseModel):
    dhlabids: List[int]
    token: str

@app.post("/api/places")
def get_places(req: PlacesRequest):
    if not req.dhlabids:
        return {"places": []}
        
    conn = get_db()
    cursor = conn.cursor()
    
    import json
    try:
        # Først, finn det absolutte antallet unike steder
        total_query = """
            WITH ids AS (
                SELECT value AS dhlabid FROM json_each(?)
            )
            SELECT COUNT(DISTINCT p.token) as total
            FROM ids
            JOIN books b ON b.dhlabid = ids.dhlabid
            JOIN places p ON p.token = b.token
            WHERE p.latitude IS NOT NULL 
                AND p.longitude IS NOT NULL
                AND p.latitude != ''
                AND p.longitude != ''
        """
        cursor.execute(total_query, [json.dumps(req.dhlabids)])
        total_places = cursor.fetchone()["total"]

        query = f"""
            WITH ids AS (
                SELECT value AS dhlabid FROM json_each(?)
            )
            SELECT 
                p.token, 
                p.modern as name, 
                CAST(p.latitude AS FLOAT) as lat, 
                CAST(p.longitude AS FLOAT) as lon, 
                COUNT(b.dhlabid) as doc_count, 
                SUM(b.book_count) as frequency
            FROM ids
            JOIN books b ON b.dhlabid = ids.dhlabid
            JOIN places p ON p.token = b.token
            WHERE p.latitude IS NOT NULL 
                AND p.longitude IS NOT NULL
                AND p.latitude != ''
                AND p.longitude != ''
            GROUP BY p.token, p.modern, p.latitude, p.longitude
            ORDER BY frequency DESC
            LIMIT ?
        """
        params = [json.dumps(req.dhlabids), req.maxPlaces]
        cursor.execute(query, params)
        rows = cursor.fetchall()
        places = [
            {"id": r["token"], "token": r["token"], "name": r["name"], "lat": r["lat"], "lon": r["lon"], 
             "frequency": r["frequency"], "doc_count": r["doc_count"]} 
            for r in rows
        ]
    except Exception as e:
        print(f"Places query error: {e}")
        places = []
        total_places = 0
    
    conn.close()
    return {"places": places, "total_places": total_places}

@app.post("/api/places/details")
def get_place_details(req: PlaceDetailsRequest):
    if not req.dhlabids:
        return {"books": []}
        
    conn = get_db()
    cursor = conn.cursor()
    
    import json
    try:
        query = f"""
            WITH ids AS (
                SELECT value AS dhlabid FROM json_each(?)
            )
            SELECT 
                c.dhlabid, c.urn, c.author, c.year, c.title, c.category,
                b.book_count as mentions
            FROM ids
            JOIN books b ON b.dhlabid = ids.dhlabid
            JOIN corpus c ON c.dhlabid = b.dhlabid
            WHERE b.token = ?
            ORDER BY b.book_count DESC
            LIMIT 500
        """
        params = [json.dumps(req.dhlabids), req.token]
        cursor.execute(query, params)
        books = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        print(f"Fetch details error: {e}")
        books = []
        
    conn.close()
    return {"books": books}

@app.post("/api/books/metadata")
def get_metadata(req: MetadataRequest):
    if not req.dhlabids:
        return {"books": []}
    
    placeholders = ",".join(["?"] * len(req.dhlabids))
    conn = get_db()
    cursor = conn.cursor()
    
    # We select standard metadata from the 'corpus' table.
    query = f"SELECT dhlabid, urn, author, year, category FROM corpus WHERE dhlabid IN ({placeholders})"
    cursor.execute(query, req.dhlabids)
    rows = cursor.fetchall()
    
    books = [dict(r) for r in rows]
    conn.close()
    return {"books": books}

@app.get("/api/metadata/all")
def get_all_metadata():
    conn = get_db()
    cursor = conn.cursor()
    query = """
        SELECT 
            c.dhlabid, c.urn, c.author, c.year, c.category, c.title, 
            COUNT(DISTINCT b.token) as unique_places, 
            SUM(b.book_count) as total_mentions
        FROM corpus c
        LEFT JOIN books b ON c.dhlabid = b.dhlabid
        GROUP BY c.dhlabid
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    
    books = [dict(r) for r in rows]
    conn.close()
    return {"books": books}

@app.get("/health")
def health():
    return {"status": "ok", "db_connected": os.path.exists(DB_PATH)}

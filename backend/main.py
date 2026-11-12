"""Servidor do aplicativo de doações (FastAPI + SQLite)."""
import base64
import hashlib
import os
import re
import secrets
import sqlite3
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_PATH = Path(__file__).parent / "doacoes.db"
app = FastAPI(title="Doe+")
# permite que o site (outra porta/endereço) carregue as fotos do servidor
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------- Banco de dados ----------
# Com a variável DATABASE_URL usa PostgreSQL (Render); sem ela, usa SQLite local.
DATABASE_URL = os.environ.get("DATABASE_URL")
USA_PG = bool(DATABASE_URL)
if USA_PG:
    import psycopg2
    import psycopg2.extras


def db():
    if USA_PG:
        return psycopg2.connect(DATABASE_URL)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def adaptar(sql):
    """No PostgreSQL o marcador de parâmetro é %s em vez de ?."""
    return sql.replace("?", "%s") if USA_PG else sql


def ler(sql, p=()):
    c = db()
    try:
        if USA_PG:
            cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(adaptar(sql), tuple(p))
            return [dict(r) for r in cur.fetchall()]
        return [dict(r) for r in c.execute(sql, tuple(p)).fetchall()]
    finally:
        c.close()


def ler1(sql, p=()):
    r = ler(sql, p)
    return r[0] if r else None


def gravar(sql, p=()):
    c = db()
    try:
        if USA_PG:
            cur = c.cursor()
            cur.execute(adaptar(sql), tuple(p))
        else:
            c.execute(sql, tuple(p))
        c.commit()
    finally:
        c.close()


def hash_senha(senha, salt):
    return hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(salt), 100_000).hex()


def criar_usuario(usuario, senha, telefone="", admin=0):
    salt = secrets.token_hex(16)
    gravar(
        "INSERT INTO usuarios(usuario, senha_hash, salt, telefone, admin) VALUES(?,?,?,?,?)",
        (usuario, hash_senha(senha, salt), salt, telefone, admin),
    )


@app.on_event("startup")
def iniciar():
    if USA_PG:
        pk = "SERIAL PRIMARY KEY"
        agora = "to_char(now() AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS')"
    else:
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
        agora = "(datetime('now','localtime'))"
    ddl = f"""
        CREATE TABLE IF NOT EXISTS usuarios(
            id {pk}, usuario TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL, salt TEXT NOT NULL, telefone TEXT DEFAULT '',
            admin INTEGER DEFAULT 0, bloqueado INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessoes(
            token TEXT PRIMARY KEY,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS itens(
            id {pk},
            doador_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            titulo TEXT NOT NULL, descricao TEXT DEFAULT '', categoria TEXT DEFAULT 'Outros',
            status TEXT DEFAULT 'disponivel', foto TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS solicitacoes(
            id {pk},
            item_id INTEGER NOT NULL REFERENCES itens(id) ON DELETE CASCADE,
            receptor_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            mensagem TEXT DEFAULT '', status TEXT DEFAULT 'pendente',
            UNIQUE(item_id, receptor_id));
        CREATE TABLE IF NOT EXISTS mensagens(
            id {pk},
            solicitacao_id INTEGER NOT NULL REFERENCES solicitacoes(id) ON DELETE CASCADE,
            autor_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            texto TEXT NOT NULL, criado TEXT DEFAULT {agora}, lida INTEGER DEFAULT 0);
    """
    c = db()
    try:
        if USA_PG:
            c.cursor().execute(ddl)
        else:
            c.executescript(ddl)
        c.commit()
    finally:
        c.close()
    # Atualiza bancos criados antes (adiciona as colunas novas, se ainda não existirem)
    for alt in (
        "ALTER TABLE itens ADD COLUMN foto TEXT DEFAULT ''",
        "ALTER TABLE mensagens ADD COLUMN lida INTEGER DEFAULT 0",
    ):
        try:
            gravar(alt)
            if "mensagens" in alt:
                gravar("UPDATE mensagens SET lida=1")
        except Exception:
            pass  # a coluna já existe
    senha_admin = os.environ.get("ADMIN_SENHA")  # online: defina ADMIN_SENHA
    if not ler1("SELECT id FROM usuarios WHERE usuario='admin'"):
        criar_usuario("admin", senha_admin or "admin123", admin=1)
    elif senha_admin:  # a variável manda: a senha do admin sempre acompanha o ADMIN_SENHA
        salt = secrets.token_hex(16)
        gravar(
            "UPDATE usuarios SET senha_hash=?, salt=? WHERE usuario='admin'",
            (hash_senha(senha_admin, salt), salt),
        )
        # encerra os logins antigos do admin (a senha mudou)
        gravar("DELETE FROM sessoes WHERE usuario_id IN (SELECT id FROM usuarios WHERE usuario='admin')")


# ---------- Autenticação ----------
def logado(authorization: str = Header(None)):
    u = ler1(
        "SELECT u.* FROM sessoes s JOIN usuarios u ON u.id=s.usuario_id WHERE s.token=?",
        (authorization or "",),
    )
    if not u:
        raise HTTPException(401, "Sessão inválida. Faça login novamente.")
    if u["bloqueado"]:
        raise HTTPException(403, "Usuário bloqueado.")
    return u


def administrador(u: dict = Depends(logado)):
    if not u["admin"]:
        raise HTTPException(403, "Apenas o administrador pode fazer isso.")
    return u


class Cadastro(BaseModel):
    usuario: str
    senha: str
    telefone: str = ""


class Login(BaseModel):
    usuario: str
    senha: str


class NovoItem(BaseModel):
    titulo: str
    descricao: str = ""
    categoria: str = "Outros"
    foto_b64: str = ""


class Solicitar(BaseModel):
    mensagem: str = ""


class Texto(BaseModel):
    texto: str


@app.post("/cadastro")
def cadastro(d: Cadastro):
    usuario = d.usuario.strip()
    if len(usuario) < 3:
        raise HTTPException(400, "O usuário precisa ter ao menos 3 caracteres.")
    if len(d.senha) < 4:
        raise HTTPException(400, "A senha precisa ter ao menos 4 caracteres.")
    if ler1("SELECT id FROM usuarios WHERE usuario=?", (usuario,)):
        raise HTTPException(400, "Esse usuário já existe.")
    tel = re.sub(r"\D", "", d.telefone)
    if not 10 <= len(tel) <= 13:
        raise HTTPException(400, "Informe um telefone válido, com DDD.")
    criar_usuario(usuario, d.senha, tel)
    return {"ok": True}


@app.post("/login")
def login(d: Login):
    u = ler1("SELECT * FROM usuarios WHERE usuario=?", (d.usuario.strip(),))
    if not u or hash_senha(d.senha, u["salt"]) != u["senha_hash"]:
        raise HTTPException(401, "Usuário ou senha inválidos.")
    if u["bloqueado"]:
        raise HTTPException(403, "Usuário bloqueado.")
    token = secrets.token_hex(24)
    gravar("INSERT INTO sessoes(token, usuario_id) VALUES(?,?)", (token, u["id"]))
    return {"token": token, "usuario": u["usuario"], "admin": bool(u["admin"])}


# ---------- Itens ----------
COLS = (
    "i.id, i.doador_id, i.titulo, i.descricao, i.categoria, i.status, "
    "CASE WHEN COALESCE(i.foto,'')='' THEN 0 ELSE 1 END AS tem_foto"
)
@app.get("/itens")
def listar_itens(busca: str = "", categoria: str = "", u: dict = Depends(logado)):
    sql = (
        "SELECT " + COLS + ", d.usuario AS doador FROM itens i JOIN usuarios d ON d.id=i.doador_id "
        "WHERE (i.status='disponivel' OR (i.doador_id=? AND i.status='reservado')) AND (LOWER(i.titulo) LIKE LOWER(?) OR LOWER(i.descricao) LIKE LOWER(?))"
    )
    p = [u["id"], f"%{busca}%", f"%{busca}%"]
    if categoria:
        sql += " AND i.categoria=?"
        p.append(categoria)
    return ler(sql + " ORDER BY i.id DESC", p)


@app.get("/meus-itens")
def meus_itens(u: dict = Depends(logado)):
    return ler(
        "SELECT " + COLS + " FROM itens i WHERE i.doador_id=? AND i.status!='doado' ORDER BY i.id DESC",
        (u["id"],),
    )


@app.post("/itens")
def novo_item(d: NovoItem, u: dict = Depends(logado)):
    if not d.titulo.strip():
        raise HTTPException(400, "Informe o título do item.")
    if len(d.foto_b64) > 1_500_000:
        raise HTTPException(400, "A foto é grande demais.")
    gravar(
        "INSERT INTO itens(doador_id, titulo, descricao, categoria, foto) VALUES(?,?,?,?,?)",
        (u["id"], d.titulo.strip(), d.descricao.strip(), d.categoria, d.foto_b64),
    )
    return {"ok": True}


@app.delete("/itens/{item_id}")
def excluir_item(item_id: int, u: dict = Depends(logado)):
    item = ler1("SELECT * FROM itens WHERE id=?", (item_id,))
    if not item or (item["doador_id"] != u["id"] and not u["admin"]):
        raise HTTPException(404, "Item não encontrado.")
    gravar("DELETE FROM itens WHERE id=?", (item_id,))
    return {"ok": True}


@app.post("/itens/{item_id}/solicitar")
def solicitar(item_id: int, d: Solicitar, u: dict = Depends(logado)):
    item = ler1("SELECT * FROM itens WHERE id=? AND status='disponivel'", (item_id,))
    if not item:
        raise HTTPException(404, "Item indisponível.")
    if item["doador_id"] == u["id"]:
        raise HTTPException(400, "Você não pode solicitar o seu próprio item.")
    if ler1("SELECT id FROM solicitacoes WHERE item_id=? AND receptor_id=?", (item_id, u["id"])):
        raise HTTPException(400, "Você já solicitou este item.")
    gravar(
        "INSERT INTO solicitacoes(item_id, receptor_id, mensagem) VALUES(?,?,?)",
        (item_id, u["id"], d.mensagem.strip()),
    )
    return {"ok": True}


@app.post("/itens/{item_id}/concluir")
def concluir(item_id: int, u: dict = Depends(logado)):
    """Doador confirma que a doação foi entregue: o item sai do aplicativo."""
    item = ler1("SELECT * FROM itens WHERE id=? AND doador_id=?", (item_id, u["id"]))
    if not item:
        raise HTTPException(404, "Item não encontrado.")
    gravar("UPDATE itens SET status='doado' WHERE id=?", (item_id,))
    return {"ok": True}


# ---------- Solicitações ----------
@app.get("/pedidos-recebidos")
def pedidos_recebidos(u: dict = Depends(logado)):
    rows = ler(
        "SELECT s.id, s.item_id, s.mensagem, s.status, i.titulo, r.usuario AS receptor, r.telefone, CASE WHEN COALESCE(i.foto,'')='' THEN 0 ELSE 1 END AS tem_foto "
        "FROM solicitacoes s JOIN itens i ON i.id=s.item_id JOIN usuarios r ON r.id=s.receptor_id "
        "WHERE i.doador_id=? AND i.status!='doado' ORDER BY s.id DESC",
        (u["id"],),
    )
    for r in rows:
        if r["status"] != "aceita":
            r["telefone"] = ""
    return rows


@app.get("/minhas-solicitacoes")
def minhas_solicitacoes(u: dict = Depends(logado)):
    rows = ler(
        "SELECT s.id, s.item_id, s.mensagem, s.status, i.titulo, d.usuario AS doador, d.telefone, CASE WHEN COALESCE(i.foto,'')='' THEN 0 ELSE 1 END AS tem_foto "
        "FROM solicitacoes s JOIN itens i ON i.id=s.item_id JOIN usuarios d ON d.id=i.doador_id "
        "WHERE s.receptor_id=? AND i.status!='doado' ORDER BY s.id DESC",
        (u["id"],),
    )
    for r in rows:
        if r["status"] != "aceita":
            r["telefone"] = ""
    return rows


def sol_do_doador(sid, u):
    s = ler1(
        "SELECT s.*, i.doador_id FROM solicitacoes s JOIN itens i ON i.id=s.item_id WHERE s.id=?",
        (sid,),
    )
    if not s or s["doador_id"] != u["id"]:
        raise HTTPException(404, "Solicitação não encontrada.")
    return s


@app.post("/solicitacoes/{sid}/aceitar")
def aceitar(sid: int, u: dict = Depends(logado)):
    s = sol_do_doador(sid, u)
    item = ler1("SELECT status FROM itens WHERE id=?", (s["item_id"],))
    if item["status"] != "disponivel":
        raise HTTPException(400, "Já existe uma doação aceita para este item. Cancele-a antes.")
    gravar("UPDATE solicitacoes SET status='aceita' WHERE id=?", (sid,))
    gravar("UPDATE itens SET status='reservado' WHERE id=?", (s["item_id"],))
    return {"ok": True}


@app.post("/solicitacoes/{sid}/cancelar")
def cancelar(sid: int, u: dict = Depends(logado)):
    """Doador ou receptor cancela uma doação já aceita: o item volta a ficar disponível."""
    s = sol_participante(sid, u)
    if s["status"] != "aceita":
        raise HTTPException(400, "Só dá para cancelar uma doação aceita.")
    gravar("UPDATE solicitacoes SET status='cancelada' WHERE id=?", (sid,))
    gravar("UPDATE itens SET status='disponivel' WHERE id=?", (s["item_id"],))
    return {"ok": True}


@app.post("/solicitacoes/{sid}/recusar")
def recusar(sid: int, u: dict = Depends(logado)):
    sol_do_doador(sid, u)
    gravar("UPDATE solicitacoes SET status='recusada' WHERE id=?", (sid,))
    return {"ok": True}


# ---------- Chat ----------
def sol_participante(sid, u):
    s = ler1(
        "SELECT s.*, i.doador_id FROM solicitacoes s JOIN itens i ON i.id=s.item_id WHERE s.id=?",
        (sid,),
    )
    if not s or u["id"] not in (s["doador_id"], s["receptor_id"]):
        raise HTTPException(404, "Conversa não encontrada.")
    return s


@app.get("/chat/{sid}")
def ler_chat(sid: int, u: dict = Depends(logado)):
    s = sol_participante(sid, u)
    gravar("UPDATE mensagens SET lida=1 WHERE solicitacao_id=? AND autor_id != ?", (sid, u["id"]))
    outro_id = s["receptor_id"] if u["id"] == s["doador_id"] else s["doador_id"]
    outro = ler1("SELECT usuario, telefone FROM usuarios WHERE id=?", (outro_id,))
    msgs = ler(
        "SELECT autor_id, texto, criado FROM mensagens WHERE solicitacao_id=? ORDER BY id", (sid,)
    )
    return {
        "outro": outro["usuario"],
        "telefone": outro["telefone"] if s["status"] == "aceita" else "",
        "mensagens": [
            {"texto": m["texto"], "criado": m["criado"], "minha": m["autor_id"] == u["id"]}
            for m in msgs
        ],
    }


@app.post("/chat/{sid}")
def enviar_chat(sid: int, d: Texto, u: dict = Depends(logado)):
    s = sol_participante(sid, u)
    if s["status"] in ("recusada", "cancelada"):
        raise HTTPException(400, "Esta solicitação foi encerrada.")
    if d.texto.strip():
        gravar(
            "INSERT INTO mensagens(solicitacao_id, autor_id, texto) VALUES(?,?,?)",
            (sid, u["id"], d.texto.strip()),
        )
    return {"ok": True}


# ---------- Administrador ----------
@app.get("/admin/usuarios")
def admin_usuarios(a: dict = Depends(administrador)):
    return ler("SELECT id, usuario, admin, bloqueado FROM usuarios ORDER BY id")


@app.post("/admin/usuarios/{uid}/bloquear")
def admin_bloquear(uid: int, a: dict = Depends(administrador)):
    if uid == a["id"]:
        raise HTTPException(400, "Você não pode bloquear a si mesmo.")
    gravar("UPDATE usuarios SET bloqueado = 1 - bloqueado WHERE id=?", (uid,))
    return {"ok": True}


@app.delete("/admin/usuarios/{uid}")
def admin_excluir(uid: int, a: dict = Depends(administrador)):
    if uid == a["id"]:
        raise HTTPException(400, "Você não pode excluir a si mesmo.")
    gravar("DELETE FROM usuarios WHERE id=?", (uid,))
    return {"ok": True}


# ---------- Site (página web) ----------
from fastapi.responses import FileResponse  # noqa: E402

SITE = Path(__file__).parent.parent / "site" / "index.html"


@app.get("/", include_in_schema=False)
def site():
    if SITE.exists():
        return FileResponse(SITE)
    return {"mensagem": "Servidor do Doe+ no ar."}


# ---------- Foto do item e novidades ----------
@app.get("/itens/{item_id}/foto", include_in_schema=False)
def foto_item(item_id: int):
    from fastapi.responses import Response

    r = ler1("SELECT foto FROM itens WHERE id=?", (item_id,))
    if not r or not r["foto"]:
        raise HTTPException(404, "Sem foto.")
    return Response(
        base64.b64decode(r["foto"]), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"}
    )


@app.get("/novidades")
def novidades(u: dict = Depends(logado)):
    """Mensagens não lidas e pedidos pendentes (usado para avisar o usuário)."""
    rows = ler(
        "SELECT m.solicitacao_id AS sid, COUNT(*) AS n FROM mensagens m "
        "JOIN solicitacoes s ON s.id=m.solicitacao_id JOIN itens i ON i.id=s.item_id "
        "WHERE m.lida=0 AND m.autor_id != ? AND i.status!='doado' "
        "AND (i.doador_id=? OR s.receptor_id=?) GROUP BY m.solicitacao_id",
        (u["id"], u["id"], u["id"]),
    )
    pend = ler1(
        "SELECT COUNT(*) AS n FROM solicitacoes s JOIN itens i ON i.id=s.item_id "
        "WHERE i.doador_id=? AND s.status='pendente' AND i.status!='doado'",
        (u["id"],),
    )
    por_chat = {str(r["sid"]): r["n"] for r in rows}
    return {"mensagens": sum(por_chat.values()), "por_chat": por_chat, "pedidos": pend["n"]}
import os
import re
import json
import random
import string
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

app = FastAPI(title="Frosty - EPI e Material de Expediente API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def init_db():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS epi_setores (
                id TEXT PRIMARY KEY,
                nome TEXT NOT NULL,
                regra_distribuicao TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS epi_itens (
                codigo TEXT PRIMARY KEY,
                descricao TEXT,
                saldo NUMERIC DEFAULT 0,
                umb TEXT,
                nome_conta TEXT,
                codigo_conta TEXT,
                familia TEXT,
                deposito TEXT,
                custo_unitario NUMERIC DEFAULT 0
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS epi_solicitacoes (
                id TEXT PRIMARY KEY,
                numero TEXT,
                solicitante TEXT NOT NULL,
                setor_id TEXT,
                setor_nome TEXT,
                regra_distribuicao TEXT,
                motivo TEXT NOT NULL,
                data_hora TEXT NOT NULL,
                status TEXT NOT NULL,
                itens_json TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS epi_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """))


@app.on_event("startup")
def on_startup():
    init_db()


def gen_id(prefix):
    return f"{prefix}_{int(datetime.now().timestamp()*1000)}_{''.join(random.choices(string.digits, k=3))}"


@app.get("/")
def root():
    return {"status": "ok", "service": "Frosty - EPI e Material de Expediente API"}


# ============================================================
#  EPI / MATERIAL DE EXPEDIENTE
# ============================================================

class ItemEpiPedido(BaseModel):
    codigo: Optional[str] = None   # vazio quando motivo = teste (item fora do catálogo)
    descricao: Optional[str] = None
    qtd: float


class NovaSolicitacaoEpi(BaseModel):
    solicitante: str
    setorId: str
    motivo: str  # "Aquisição" | "Troca" | "Teste"
    itens: list[ItemEpiPedido]


class NovoItemEpi(BaseModel):
    codigo: str
    descricao: str
    umb: Optional[str] = "UN"
    nomeConta: Optional[str] = ""
    codigoConta: Optional[str] = ""
    familia: Optional[str] = "GERAL"
    deposito: Optional[str] = ""
    custoUnitario: Optional[float] = 0
    saldo: Optional[float] = 0


class StatusEpiUpdate(BaseModel):
    status: str


class JanelaDia(BaseModel):
    dia: int
    horaInicio: int
    minutoInicio: int = 0
    horaFim: int
    minutoFim: int = 0


class JanelaEpi(BaseModel):
    dias: list[JanelaDia]


class TesteAbertoEpi(BaseModel):
    ligado: bool


@app.get("/api/epi/data")
def epi_get_all_data():
    with engine.begin() as conn:
        setores = [dict(r._mapping) for r in conn.execute(text(
            "SELECT id, nome, regra_distribuicao as \"regraDistribuicao\" FROM epi_setores ORDER BY nome"))]
        itens = []
        for r in conn.execute(text(
                "SELECT codigo, descricao, saldo, umb, nome_conta as \"nomeConta\", codigo_conta as \"codigoConta\", "
                "familia, deposito, custo_unitario as \"custoUnitario\" FROM epi_itens ORDER BY familia, descricao")):
            row = dict(r._mapping)
            row["saldo"] = float(row["saldo"]) if row["saldo"] is not None else 0
            itens.append(row)

        solicitacoes = []
        for r in conn.execute(text(
                "SELECT id, numero, solicitante, setor_id as \"setorId\", setor_nome as \"setorNome\", "
                "regra_distribuicao as \"regraDistribuicao\", motivo, data_hora as \"dataHora\", status, itens_json "
                "FROM epi_solicitacoes ORDER BY data_hora DESC")):
            row = dict(r._mapping)
            row["itens"] = json.loads(row.pop("itens_json"))
            solicitacoes.append(row)

        janela = {"dias": [
            {"dia": 2, "horaInicio": 8, "minutoInicio": 0, "horaFim": 18, "minutoFim": 0},
            {"dia": 4, "horaInicio": 8, "minutoInicio": 0, "horaFim": 18, "minutoFim": 0},
            {"dia": 5, "horaInicio": 8, "minutoInicio": 0, "horaFim": 18, "minutoFim": 0},
        ]}
        for r in conn.execute(text("SELECT value FROM epi_config WHERE key='janela'")):
            janela = json.loads(r.value)

        teste_aberto = False
        for r in conn.execute(text("SELECT value FROM epi_config WHERE key='testeAberto'")):
            teste_aberto = r.value == "true"

    return {"setores": setores, "itens": itens, "solicitacoes": solicitacoes, "janela": janela, "testeAberto": teste_aberto}


@app.post("/api/epi/solicitacoes")
def epi_criar_solicitacao(payload: NovaSolicitacaoEpi):
    with engine.begin() as conn:
        setor = conn.execute(text(
            "SELECT id, nome, regra_distribuicao FROM epi_setores WHERE id = :id"), {"id": payload.setorId}).fetchone()
        if not setor:
            raise HTTPException(404, "Setor não encontrado.")

        itens_resp = []
        eh_teste = payload.motivo.strip().lower() == "teste"

        for item in payload.itens:
            if not item.qtd or item.qtd <= 0:
                continue
            if eh_teste:
                # motivo teste: item precisa existir no catálogo (código/conta/umb reais), mas sem controle de estoque
                if not item.codigo:
                    raise HTTPException(400, "Selecione um item da lista de sugestões (mesmo em modo teste, o item precisa estar cadastrado).")
                row = conn.execute(text(
                    "SELECT descricao, umb, codigo_conta, nome_conta FROM epi_itens WHERE codigo = :cod"
                ), {"cod": item.codigo}).fetchone()
                if not row:
                    raise HTTPException(400, f"Item {item.codigo} não encontrado no catálogo. Escolha um item da lista de sugestões.")
                itens_resp.append({
                    "codigo": item.codigo, "descricao": row.descricao, "umb": row.umb,
                    "qtd": item.qtd, "codigoConta": row.codigo_conta, "nomeConta": row.nome_conta,
                })
            else:
                row = conn.execute(text(
                    "SELECT descricao, umb, saldo, codigo_conta, nome_conta FROM epi_itens WHERE codigo = :cod FOR UPDATE"
                ), {"cod": item.codigo}).fetchone()
                if not row:
                    continue
                saldo_antes = float(row.saldo)
                if item.qtd > saldo_antes:
                    raise HTTPException(400, f"O item {item.codigo} ({row.descricao}) excede o saldo disponível ({saldo_antes}). Reduza a quantidade.")
                saldo_depois = saldo_antes - item.qtd
                conn.execute(text(
                    "UPDATE epi_itens SET saldo = :novo WHERE codigo = :cod"
                ), {"novo": saldo_depois, "cod": item.codigo})
                itens_resp.append({
                    "codigo": item.codigo, "descricao": row.descricao, "umb": row.umb, "qtd": item.qtd,
                    "codigoConta": row.codigo_conta, "nomeConta": row.nome_conta,
                })

        if not itens_resp:
            raise HTTPException(400, "Nenhum item válido selecionado.")

        now = datetime.now(timezone.utc)
        numero = "EPI-" + now.strftime("%y%m%d") + "-" + "".join(random.choices(string.digits, k=3))
        sol_id = gen_id("episol")
        solicitacao = {
            "id": sol_id, "numero": numero, "solicitante": payload.solicitante,
            "setorId": setor.id, "setorNome": setor.nome, "regraDistribuicao": setor.regra_distribuicao,
            "motivo": payload.motivo, "dataHora": now.isoformat(), "status": "Aguardando Separação",
            "itens": itens_resp,
        }
        conn.execute(text(
            "INSERT INTO epi_solicitacoes (id, numero, solicitante, setor_id, setor_nome, regra_distribuicao, "
            "motivo, data_hora, status, itens_json) VALUES "
            "(:id,:numero,:solicitante,:setor_id,:setor_nome,:regra,:motivo,:data_hora,:status,:itens_json)"
        ), {
            "id": sol_id, "numero": numero, "solicitante": payload.solicitante, "setor_id": setor.id,
            "setor_nome": setor.nome, "regra": setor.regra_distribuicao, "motivo": payload.motivo,
            "data_hora": now.isoformat(), "status": "Aguardando Separação", "itens_json": json.dumps(itens_resp),
        })

    return solicitacao


@app.patch("/api/epi/solicitacoes/{sol_id}/status")
def epi_atualizar_status(sol_id: str, body: StatusEpiUpdate):
    with engine.begin() as conn:
        result = conn.execute(text(
            "UPDATE epi_solicitacoes SET status = :status WHERE id = :id"
        ), {"status": body.status, "id": sol_id})
        if result.rowcount == 0:
            raise HTTPException(404, "Solicitação não encontrada.")
    return {"ok": True}


@app.put("/api/epi/config/janela")
def epi_salvar_janela(body: JanelaEpi):
    with engine.begin() as conn:
        ok = conn.execute(text(
            "UPDATE epi_config SET value = :v WHERE key = 'janela'"
        ), {"v": json.dumps(body.dict())})
        if ok.rowcount == 0:
            conn.execute(text("INSERT INTO epi_config (key, value) VALUES ('janela', :v)"), {"v": json.dumps(body.dict())})
    return {"ok": True}


@app.put("/api/epi/config/teste")
def epi_salvar_teste_aberto(body: TesteAbertoEpi):
    with engine.begin() as conn:
        valor = "true" if body.ligado else "false"
        ok = conn.execute(text(
            "UPDATE epi_config SET value = :v WHERE key = 'testeAberto'"
        ), {"v": valor})
        if ok.rowcount == 0:
            conn.execute(text("INSERT INTO epi_config (key, value) VALUES ('testeAberto', :v)"), {"v": valor})
    return {"ok": True}


@app.get("/api/epi/solicitacoes/{sol_id}/pdf")
def epi_gerar_pdf(sol_id: str):
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle
    from reportlab.pdfgen import canvas
    from io import BytesIO
    from fastapi.responses import StreamingResponse

    with engine.begin() as conn:
        row = conn.execute(text("SELECT * FROM epi_solicitacoes WHERE id = :id"), {"id": sol_id}).fetchone()
    if not row:
        raise HTTPException(404, "Solicitação não encontrada.")
    itens = json.loads(row.itens_json)
    dt = datetime.fromisoformat(row.data_hora)

    AZUL = colors.HexColor("#1B2FE0")
    VERDE = colors.HexColor("#3FC340")
    CINZA_HEADER = colors.HexColor("#D9E1F2")

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=landscape(A4))
    largura, altura = landscape(A4)
    margem = 1.6*cm

    # ---- cabeçalho azul com marca "frosty" ----
    banner_h = 1.7*cm
    c.setFillColor(AZUL)
    c.rect(0, altura-banner_h, largura, banner_h, fill=1, stroke=0)
    c.setFillColor(VERDE)
    c.roundRect(margem, altura-banner_h+0.3*cm, 2.8*cm, 1.1*cm, 8, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(margem+1.4*cm, altura-banner_h+0.65*cm, "frosty")
    c.setFont("Helvetica-Bold", 17)
    c.drawCentredString(largura/2, altura-banner_h+0.62*cm, "ATENDIMENTO DE RESERVAS")

    # ---- dados do cabeçalho ----
    y = altura - banner_h - 0.9*cm
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margem, y, "SETOR:")
    c.setFont("Helvetica", 10)
    c.drawString(margem+1.8*cm, y, row.setor_nome or "")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(largura/2, y, "Solicitante:")
    c.setFont("Helvetica", 10)
    c.drawString(largura/2+2.4*cm, y, row.solicitante)

    y -= 0.55*cm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margem, y, "CC:")
    c.setFont("Helvetica", 10)
    c.drawString(margem+1.8*cm, y, str(row.regra_distribuicao or "-"))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(largura/2, y, "Nº solicitação:")
    c.setFont("Helvetica", 10)
    c.drawString(largura/2+2.8*cm, y, row.numero)

    y -= 1*cm

    # ---- tabela de itens ----
    data = [["ITEM", "CÓDIGO", "DESCRIÇÃO DO MATERIAL", "UMB", "QNTD", "COD CONTA", "ATENDIDA"]]
    for i, it in enumerate(itens, start=1):
        data.append([
            str(i), it.get("codigo") or "-", (it.get("descricao") or "")[:80],
            it.get("umb") or "-", str(it.get("qtd")), it.get("codigoConta") or "-", "S (   )   N (   )"
        ])

    col_widths = [1.2*cm, 2.6*cm, 12.5*cm, 1.6*cm, 1.6*cm, 3.0*cm, 3.6*cm]
    tbl = Table(data, colWidths=col_widths)
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), CINZA_HEADER),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8.5),
        ('GRID', (0,0), (-1,-1), 0.6, AZUL),
        ('ALIGN', (0,0), (0,-1), 'CENTER'),
        ('ALIGN', (3,0), (4,-1), 'CENTER'),
        ('ALIGN', (6,0), (6,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    tw, th = tbl.wrapOn(c, largura-2*margem, altura)
    if th > y - 4*cm:  # não cabe tudo na página: começa de novo com margem cheia (pedidos muito grandes)
        c.showPage()
        y = altura - margem
    tbl.drawOn(c, margem, y-th)
    y = y - th - 1.1*cm

    # ---- rodapé: assinatura e motivo ----
    if y < 3*cm:
        c.showPage(); y = altura - margem
    c.setFont("Helvetica", 10)
    c.drawString(margem, y, "Entregue por: ______________________________________")
    c.drawString(margem+10.5*cm, y, f"Motivo: {row.motivo}")
    y -= 0.7*cm
    c.drawString(margem, y, f"Data da solicitação: {dt.strftime('%d/%m/%Y')}")

    c.showPage()
    c.save()
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="solicitacao_{row.numero}.pdf"'
    })


@app.post("/api/epi/itens")
def epi_salvar_item(body: NovoItemEpi):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO epi_itens (codigo, descricao, saldo, umb, nome_conta, codigo_conta, familia, deposito, custo_unitario) "
            "VALUES (:codigo,:descricao,:saldo,:umb,:nomeConta,:codigoConta,:familia,:deposito,:custoUnitario) "
            "ON CONFLICT (codigo) DO UPDATE SET descricao=:descricao, saldo=:saldo, umb=:umb, nome_conta=:nomeConta, "
            "codigo_conta=:codigoConta, familia=:familia, deposito=:deposito, custo_unitario=:custoUnitario"
        ), body.dict())
    return {"ok": True}


@app.post("/api/epi/itens/importar")
async def epi_importar_itens(file: UploadFile):
    import openpyxl
    from io import BytesIO

    conteudo = await file.read()
    try:
        wb = openpyxl.load_workbook(BytesIO(conteudo), data_only=True)
    except Exception:
        raise HTTPException(400, "Não consegui abrir o arquivo. Confirme que é um .xlsx válido.")

    ws = wb[wb.sheetnames[0]]  # usa a primeira aba, mesmo formato do Cadastro_dos_itens.xlsx / ESTOQUE.xlsx
    rows = list(ws.iter_rows(min_row=2, values_only=True))

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM epi_itens"))  # substitui o catálogo inteiro, igual ao dos secos
        total = 0
        for r in rows:
            if not r or r[0] is None:
                continue
            conn.execute(text(
                "INSERT INTO epi_itens (codigo, descricao, saldo, umb, nome_conta, codigo_conta, familia, deposito, custo_unitario) "
                "VALUES (:codigo,:descricao,:saldo,:umb,:nomeConta,:codigoConta,:familia,:deposito,:custoUnitario) "
                "ON CONFLICT (codigo) DO UPDATE SET descricao=:descricao, saldo=:saldo, umb=:umb, nome_conta=:nomeConta, "
                "codigo_conta=:codigoConta, familia=:familia, deposito=:deposito, custo_unitario=:custoUnitario"
            ), {
                "codigo": str(r[0]).strip(),
                "descricao": (r[1] or "").strip() if r[1] else "",
                "saldo": r[2] if r[2] is not None else 0,
                "umb": r[3] or "UN",
                "nomeConta": (r[4] or "").strip() if r[4] else "",
                "codigoConta": (r[5] or "").strip() if r[5] else "",
                "familia": (r[6] or "").strip() if r[6] else "",
                "deposito": str(r[7]) if r[7] is not None else "",
                "custoUnitario": r[8] if r[8] is not None else 0,
            })
            total += 1

    return {"ok": True, "itens_importados": total}


@app.get("/api/epi/seed-inicial")
def epi_seed_inicial():
    from epi_seed_data import EPI_SETORES_SEED, EPI_ITENS_SEED
    with engine.begin() as conn:
        ja_tem = conn.execute(text("SELECT COUNT(*) FROM epi_setores")).scalar()
        if ja_tem and ja_tem > 0:
            return {"ok": False, "mensagem": "Já existem dados de EPI — nada foi alterado."}

        for i, s in enumerate(EPI_SETORES_SEED):
            conn.execute(text(
                "INSERT INTO epi_setores (id, nome, regra_distribuicao) VALUES (:id,:nome,:regra)"
            ), {"id": f"setor_{i}", "nome": s["nome"], "regra": s["regraDistribuicao"]})

        for it in EPI_ITENS_SEED:
            conn.execute(text(
                "INSERT INTO epi_itens (codigo, descricao, saldo, umb, nome_conta, codigo_conta, familia, deposito, custo_unitario) "
                "VALUES (:codigo,:descricao,:saldo,:umb,:nomeConta,:codigoConta,:familia,:deposito,:custoUnitario) "
                "ON CONFLICT (codigo) DO NOTHING"
            ), it)

        conn.execute(text(
            "INSERT INTO epi_config (key, value) VALUES ('janela', :v) ON CONFLICT (key) DO NOTHING"
        ), {"v": json.dumps({"dias": [
            {"dia": 2, "horaInicio": 8, "minutoInicio": 0, "horaFim": 18, "minutoFim": 0},
            {"dia": 4, "horaInicio": 8, "minutoInicio": 0, "horaFim": 18, "minutoFim": 0},
            {"dia": 5, "horaInicio": 8, "minutoInicio": 0, "horaFim": 18, "minutoFim": 0},
        ]})})

    return {"ok": True, "setores": len(EPI_SETORES_SEED), "itens": len(EPI_ITENS_SEED)}


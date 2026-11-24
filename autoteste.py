#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autoteste.py
============
Testa o programa NESTE computador, do começo ao fim, sem tocar nos seus
arquivos: cria PDFs de teste numa pasta temporária, separa em lotes,
remonta e compara com os originais.

Roda tudo duas vezes: pacotes em PDF único (padrão) e em ZIP.

Casos testados
  * PDFs pequenos com nomes numerados (ordem natural: 2 antes de 10)
  * PDF grande com sumário em vários níveis e links internos (diretos,
    por ação e por nome) que cruzam de uma parte para outra
  * página única gigante com imagem sem perda e transparência (faixas)
  * página única gigante em JPEG (deve ir para as exceções)
  * PDF com assinatura digital (deve ir para as exceções)

Uso:  python autoteste.py            (a interface tem um botão para isto)
"""

import argparse
import csv
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

try:
    import pikepdf
    from pikepdf import Array, Dictionary, Name, String
except ImportError:
    sys.exit("Falta a biblioteca pikepdf. Instale com:\n    pip install pikepdf")

PASTA = Path(__file__).resolve().parent
sys.path.insert(0, str(PASTA))
import nucleo_pdf as nucleo  # noqa: E402

MIB = 1024 * 1024
META_MB = 5          # pacotes pequenos deixam o teste rápido; a lógica é a mesma de 47,5 MB


def progresso(args, etapa, atual, total, texto=""):
    if args.progresso:
        print(f"@@PROGRESSO|{etapa}|{atual}|{total}|{texto}", flush=True)


# --------------------------------------------------------------------------
# Geração dos PDFs de teste
# --------------------------------------------------------------------------
def imagem_aleatoria(pdf, largura, altura, cores=1):
    s = pikepdf.Stream(pdf, os.urandom(largura * altura * cores))
    s.Type, s.Subtype = Name.XObject, Name.Image
    s.Width, s.Height, s.BitsPerComponent = largura, altura, 8
    s.ColorSpace = Name.DeviceRGB if cores == 3 else Name.DeviceGray
    return s


def imagem_jpeg(pdf, lado):
    from PIL import Image
    buf = io.BytesIO()
    Image.frombytes("L", (lado, lado), os.urandom(lado * lado)).save(buf, "JPEG", quality=95)
    s = pikepdf.Stream(pdf, buf.getvalue())
    s.Type, s.Subtype, s.Filter = Name.XObject, Name.Image, Name.DCTDecode
    s.Width, s.Height, s.BitsPerComponent, s.ColorSpace = lado, lado, 8, Name.DeviceGray
    return s


def pagina_com_imagem(pdf, img, texto=""):
    pdf.add_blank_page(page_size=(595, 842))
    pg = pdf.pages[-1]
    pg.obj.Resources = Dictionary(XObject=Dictionary(Im0=img),
                                  Font=Dictionary(F1=Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                                                BaseFont=Name.Helvetica)))
    conteudo = "q 595 0 0 842 0 0 cm /Im0 Do Q"
    if texto:
        conteudo += f" BT /F1 18 Tf 40 800 Td ({texto}) Tj ET"
    pg.obj.Contents = pdf.make_stream(conteudo.encode("latin-1"))
    return pg


def gerar(pasta: Path, meta: int):
    random.seed(20261114)
    arquivos = {}

    # 1) pequenos
    for n in range(1, 16):
        pdf = pikepdf.Pdf.new()
        tam = int(meta * random.choice([0.03, 0.08, 0.15, 0.3, 0.5]))
        lado = int((tam / 2) ** 0.5)
        for k in range(2):
            pagina_com_imagem(pdf, imagem_aleatoria(pdf, lado, lado), f"Processo {n} pag {k + 1}")
        # data de criação registrada no PDF em ordem INVERSA à do nome
        # (processo_15 é o mais antigo): testa a ordem por data de criação
        dia = 16 - n
        if n % 2:      # data no dicionário Info
            pdf.docinfo["/CreationDate"] = f"D:202103{dia:02d}090000-03'00'"
        else:          # data só no XMP
            with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as xmp:
                xmp["xmp:CreateDate"] = f"2021-03-{dia:02d}T09:00:00-03:00"
        destino = pasta / "processos" / f"processo_{n}.pdf"     # sem zeros: testa ordem natural
        destino.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(destino)
        arquivos[destino] = "inteiro"

    # 1b) medições com nomes diferentes (a ordem tem de vir do número da medição)
    for nome in ["Relatorio_medicao_3.pdf", "Boletim Medição 1.pdf", "2ª medição - fotos.pdf",
                 "BM 10 - planilha.pdf", "Anexo sem numero.pdf", "Med-04 Pavimentacao.pdf"]:
        pdf = pikepdf.Pdf.new()
        lado = int((meta * 0.05) ** 0.5)
        pagina_com_imagem(pdf, imagem_aleatoria(pdf, lado, lado), "Medicao")
        destino = pasta / "medicoes" / nome
        destino.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(destino)
        arquivos[destino] = "inteiro"

    # 2) grande com sumário e links
    pdf = pikepdf.Pdf.new()
    lado = int((meta * 0.11) ** 0.5)
    for k in range(40):
        pagina_com_imagem(pdf, imagem_aleatoria(pdf, lado, lado), f"Contrato pagina {k + 1}")
    pdf.Root.Names = Dictionary(Dests=Dictionary(Names=Array([
        String("anexo"), Array([pdf.pages[37].obj, Name.XYZ, 0, 700, 0])])))
    for k, pg in enumerate(pdf.pages):
        alvo = pdf.pages[(k + 13) % 40].obj
        pg.obj.Annots = pdf.make_indirect(Array([
            pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Link, Rect=[40, 40, 140, 60],
                                         Dest=Array([alvo, Name.XYZ, 0, 500, 0]))),
            pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Link, Rect=[40, 70, 140, 90],
                                         A=Dictionary(S=Name.GoTo, D=String("anexo")))),
            pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Link, Rect=[40, 100, 140, 120],
                                         A=Dictionary(S=Name.GoTo,
                                                      D=Array([pdf.pages[(k * 7) % 40].obj, Name.Fit])))),
        ]))
    with pdf.open_outline() as o:
        for c in range(4):
            cap = pikepdf.OutlineItem(f"Capítulo {c + 1}", c * 10)
            for s in range(3):
                sec = pikepdf.OutlineItem(f"Seção {c + 1}.{s + 1}", c * 10 + s * 3 + 1)
                sec.children.append(pikepdf.OutlineItem(f"Item {c + 1}.{s + 1}.1", c * 10 + s * 3 + 2))
                cap.children.append(sec)
            cap.is_closed = c % 2 == 1
            o.root.append(cap)
        o.root.append(pikepdf.OutlineItem("Anexo (por nome)", String("anexo")))
    pdf.docinfo["/Title"] = "Contrato de teste com navegação"
    destino = pasta / "contratos" / "contrato_navegavel.pdf"
    destino.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(destino)
    arquivos[destino] = "cortado"

    # 3) página gigante sem perda (RGB + transparência) entre páginas normais
    pdf = pikepdf.Pdf.new()
    pagina_com_imagem(pdf, imagem_aleatoria(pdf, 300, 300), "Planta - capa")
    w = int((meta * 1.6 / 3) ** 0.5)
    h = int(w * 1.3)
    grande = imagem_aleatoria(pdf, w, h, cores=3)
    grad = bytes((y * 255 // h) for y in range(h) for _ in range(w))
    smask = pikepdf.Stream(pdf, grad)
    smask.Type, smask.Subtype, smask.Width, smask.Height = Name.XObject, Name.Image, w, h
    smask.BitsPerComponent, smask.ColorSpace = 8, Name.DeviceGray
    grande.SMask = smask
    pg = pagina_com_imagem(pdf, grande, "Planta - folha unica")
    pg.obj.Annots = pdf.make_indirect(Array([pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Link, Rect=[40, 40, 140, 60],
        Dest=Array([pdf.pages[0].obj, Name.Fit])))]))
    pagina_com_imagem(pdf, imagem_aleatoria(pdf, 300, 300), "Planta - verso")
    with pdf.open_outline() as o:
        o.root.extend([pikepdf.OutlineItem("Capa", 0), pikepdf.OutlineItem("Planta", 1),
                       pikepdf.OutlineItem("Verso", 2)])
    destino = pasta / "obras" / "planta_sem_perda.pdf"
    destino.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(destino)
    arquivos[destino] = "cortado"

    # 4) página gigante em JPEG -> exceção
    pdf = pikepdf.Pdf.new()
    pagina_com_imagem(pdf, imagem_jpeg(pdf, int((meta * 1.25) ** 0.5)), "Foto")
    destino = pasta / "obras" / "foto_jpeg_gigante.pdf"
    pdf.save(destino)
    arquivos[destino] = "excecao"

    # 5) assinado digitalmente -> exceção
    pdf = pikepdf.Pdf.new()
    lado = int((meta * 0.5) ** 0.5)
    for k in range(5):
        pagina_com_imagem(pdf, imagem_aleatoria(pdf, lado, lado), f"Assinado {k + 1}")
    campo = pdf.make_indirect(Dictionary(
        FT=Name.Sig, T=String("Assinatura1"),
        V=Dictionary(Type=Name.Sig, Filter=Name("/Adobe.PPKLite"),
                     SubFilter=Name("/adbe.pkcs7.detached"), Contents=String(b"\x00" * 64))))
    pdf.Root.AcroForm = Dictionary(Fields=Array([campo]), SigFlags=3)
    destino = pasta / "assinado.pdf"
    pdf.save(destino)
    arquivos[destino] = "excecao"

    # datas variadas
    base = time.mktime((2022, 1, 3, 9, 0, 0, 0, 0, -1))
    for f in arquivos:
        t = base + random.randint(0, 3 * 365) * 86400 + random.randint(0, 86399)
        os.utime(f, (t, t))
    return arquivos


# --------------------------------------------------------------------------
# Comparação
# --------------------------------------------------------------------------
def _sha(b):
    return hashlib.sha256(b).hexdigest()[:16]


def _conteudo(obj):
    c = obj.get("/Contents")
    if c is None:
        return b""
    if isinstance(c, Array):
        return b"".join(x.read_bytes() for x in c)
    return c.read_bytes()


def _dados_imagem(img):
    filtros = nucleo._filtros(img)
    if all(f in nucleo.FILTROS_SEM_PERDA for f in filtros):
        return _sha(img.read_bytes())
    return _sha(img.read_raw_bytes())


def imagens_desenhadas(obj, conteudo, profundidade=0):
    """Pixels de cada imagem efetivamente desenhada (entra em formulários)."""
    res = obj.get("/Resources")
    xo = res.get("/XObject") if isinstance(res, Dictionary) else None
    saida = []
    for nome in re.findall(rb"/([^\s/\[\]<>()]+)\s+Do\b", conteudo):
        alvo = xo.get("/" + nome.decode("latin-1")) if isinstance(xo, Dictionary) else None
        if not isinstance(alvo, pikepdf.Stream):
            continue
        if alvo.get("/Subtype") == "/Image":
            m = alvo.get("/SMask")
            saida.append((_dados_imagem(alvo), int(alvo.Height),
                          _dados_imagem(m) if isinstance(m, pikepdf.Stream) else None))
        elif alvo.get("/Subtype") == "/Form" and profundidade < 5:
            saida += imagens_desenhadas(alvo, alvo.read_bytes(), profundidade + 1)
    return saida


def links(pdf):
    idx = nucleo.indice_paginas(pdf)
    out = []
    for p in pdf.pages:
        lista = []
        for a in nucleo.anotacoes(p.obj):
            d = nucleo.destino_do_link(a) if isinstance(a, Dictionary) else None
            if d is not None:
                lista.append((tuple(float(x) for x in a.Rect), nucleo.resolver_destino(pdf, idx, d)[0]))
        out.append(lista)
    return out


def comparar(original: Path, remontado: Path):
    problemas = []
    if abs(original.stat().st_mtime - remontado.stat().st_mtime) > 2:
        problemas.append("data de modificação diferente")
    if original.read_bytes() == remontado.read_bytes():
        return problemas, "idêntico byte a byte"
    with pikepdf.open(original) as a, pikepdf.open(remontado) as b:
        if len(a.pages) != len(b.pages):
            return problemas + [f"páginas: {len(a.pages)} x {len(b.pages)}"], ""
        for i, (pa, pb) in enumerate(zip(a.pages, b.pages), 1):
            ca, cb = _conteudo(pa.obj), _conteudo(pb.obj)
            if ca != cb:
                problemas.append(f"conteúdo da página {i} diferente")
            if imagens_desenhadas(pa.obj, ca) != imagens_desenhadas(pb.obj, cb):
                problemas.append(f"imagens (pixels) da página {i} diferentes")
        if nucleo.ler_marcadores(a, nucleo.indice_paginas(a)) != \
                nucleo.ler_marcadores(b, nucleo.indice_paginas(b)):
            problemas.append("sumário (marcadores) diferente")
        if links(a) != links(b):
            problemas.append("links internos diferentes")
        if str(a.docinfo.get("/Title", "")) != str(b.docinfo.get("/Title", "")):
            problemas.append("título do documento diferente")
    return problemas, "mesmas páginas, pixels, sumário e links"


# --------------------------------------------------------------------------
# Execução
# --------------------------------------------------------------------------
def rodar(script, *args):
    r = subprocess.run([sys.executable, str(PASTA / script), *map(str, args)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    return r.returncode, r.stdout + r.stderr


def conferir_rodada(formato, origem, esperado, raiz, checar, args, etapa0):
    """Uma rodada completa (separar, conferir, remontar, comparar) num formato."""
    saida, volta = raiz / f"saida_{formato}", raiz / f"remontados_{formato}"
    pacotes_dir = saida / "Pacotes_Agrupados"
    meta = int(META_MB * 1_000_000)
    ext = "pdf" if formato == "pdf" else "zip"
    rotulo = "PDF único" if formato == "pdf" else "ZIP"

    progresso(args, "teste", etapa0, 7, f"agrupando ({rotulo})")
    print(f"\n== Formato {rotulo}: regra 1 + regra 2, teto de {META_MB} MB ==", flush=True)
    cod, log = rodar("organizar_pdfs.py", origem, saida, "--meta-mb", META_MB,
                     "--unidade", "decimal", "--formato", formato, "--ordem", "natural")
    checar("particionamento e agrupamento terminaram sem erro", cod == 0,
           "" if cod == 0 else log[-800:])
    pacotes = sorted(pacotes_dir.glob(f"Pacote_*.{ext}"))
    maior = max((z.stat().st_size for z in pacotes), default=0)
    checar("teto de segurança: nenhum pacote acima do teto", bool(pacotes) and maior <= meta,
           f"{len(pacotes)} pacotes em Pacotes_Agrupados, maior " +
           f"{maior:,} bytes".replace(",", "."))
    checar("isolamento: originais intactos", all(
        f.exists() and f.stat().st_mtime == esperado[f][1] and f.stat().st_size == esperado[f][2]
        for f in esperado))

    linhas = list(csv.DictReader(open(saida / "indice.csv", encoding="utf-8-sig"),
                                 delimiter=";")) if (saida / "indice.csv").exists() else []
    import organizar_pdfs as org
    chaves = [(org.chave_caminho(r["documento_original"]),
               int(r["parte"].split("/")[0]) if r["parte"] else 0) for r in linhas]
    sequencia = [int(re.search(r"\d+", r["pacote"]).group()) for r in linhas]
    pos = {Path(r["documento_original"]).name: k for k, r in reversed(list(enumerate(linhas)))}
    checar("ordenação natural (processo_2 antes de processo_10, parte 1 antes da 2)",
           chaves == sorted(chaves) and sequencia == sorted(sequencia)
           and pos.get("processo_2.pdf", 1) < pos.get("processo_10.pdf", 0))
    excs = list(csv.DictReader(open(saida / "excecoes.csv", encoding="utf-8-sig"),
                               delimiter=";")) if (saida / "excecoes.csv").exists() else []
    txt_exc = " ".join(e["documento_original"] + " " + e["motivo"] for e in excs)
    checar("PDF assinado não foi particionado (exceção)",
           "assinado.pdf" in txt_exc and "ASSINATURA" in txt_exc)
    checar("página gigante em JPEG foi para as exceções", "foto_jpeg_gigante.pdf" in txt_exc)
    faixas = [r for r in linhas if "faixa" in r["paginas_no_original"]]
    checar("página gigante sem perda dividida em faixas", len(faixas) >= 2,
           f"{len(faixas)} faixas")

    progresso(args, "teste", etapa0 + 1, 7, f"conferindo navegação ({rotulo})")
    nomes = {p.name for p in pacotes} if formato == "pdf" else \
        {Path(r["arquivo"]).name for r in linhas if r["parte"]}
    remotos, quebrados, sumarios, marcadores_ok = 0, 0, [], True

    def examinar(pdf, so_contrato_pags=None):
        nonlocal remotos, quebrados
        for k, pg in enumerate(pdf.pages):
            if so_contrato_pags is not None and k not in so_contrato_pags:
                continue
            for a in nucleo.anotacoes(pg.obj):
                acao = a.get("/A")
                if isinstance(acao, Dictionary) and acao.get("/S") == "/GoToR":
                    remotos += 1
                    quebrados += str(acao.F) not in nomes
                elif "/Dest" in a and (a.Dest is None or not isinstance(a.Dest[0], Dictionary)):
                    quebrados += 1

    if formato == "pdf":
        for pac in pacotes:
            with pikepdf.open(pac) as p:
                docs = [r for r in linhas if r["pacote"] == pac.name]
                with p.open_outline() as o:
                    titulos = [i.title for i in o.root]
                    if titulos != [Path(r["arquivo"]).name for r in docs]:
                        marcadores_ok = False
                    for item, r in zip(o.root, docs):
                        if r["documento_original"].endswith("contrato_navegavel.pdf"):
                            sumarios.append(len(item.children))
                pags = set()
                for r in docs:
                    if r["documento_original"].endswith("contrato_navegavel.pdf"):
                        a, b = map(int, r["paginas_no_pacote"].split("-"))
                        pags |= set(range(a - 1, b))
                if pags:
                    examinar(p, pags)
        checar("um marcador por documento em cada pacote", marcadores_ok)
    else:
        for r in linhas:
            if not r["documento_original"].endswith("contrato_navegavel.pdf"):
                continue
            with zipfile.ZipFile(pacotes_dir / r["pacote"]) as z:
                with pikepdf.open(io.BytesIO(z.read(r["arquivo"]))) as p:
                    with p.open_outline() as o:
                        sumarios.append(len(o.root))
                    examinar(p)
        checar("ZIPs sem compressão e íntegros", all(
            zipfile.ZipFile(z).testzip() is None and
            all(i.compress_type == zipfile.ZIP_STORED for i in zipfile.ZipFile(z).infolist())
            for z in pacotes))
    checar("sumário completo do documento em cada volume", bool(sumarios) and
           all(s == 5 for s in sumarios), f"{len(sumarios)} volumes do contrato")
    checar("links entre volumes apontam para o arquivo certo", remotos > 0 and quebrados == 0,
           f"{remotos} links para outros arquivos, {quebrados} quebrados")

    progresso(args, "teste", etapa0 + 2, 7, f"remontando e comparando ({rotulo})")
    cod, log = rodar("remontar_pdfs.py", pacotes_dir if formato == "pdf" else saida, volta)
    checar("remontagem terminou sem erro", cod == 0, "" if cod == 0 else log[-800:])
    if formato == "pdf":
        # os excecoes ficam na pasta de saída; remonta também a partir dela
        pass
    for f, (tipo, _, _) in sorted(esperado.items()):
        if tipo == "excecao":
            continue
        rel = f.relative_to(origem)
        r = volta / rel
        if not r.exists():
            checar(f"{rel.as_posix()}", False, "não foi remontado")
            continue
        prob, como = comparar(f, r)
        if tipo == "cortado" or prob:
            checar(f"{rel.as_posix()}", not prob, "; ".join(prob) or como)
    inteiros = [f for f, t in esperado.items() if t[0] == "inteiro"]
    if formato == "zip":
        iguais = sum((volta / f.relative_to(origem)).exists() and
                     (volta / f.relative_to(origem)).read_bytes() == f.read_bytes()
                     for f in inteiros)
        checar("PDFs não particionados voltam idênticos byte a byte",
               iguais == len(inteiros), f"{iguais} de {len(inteiros)}")

        # passo final da remontagem: zipar o resultado (e manter só os .zip)
        volta_zip = raiz / "remontados_zipados"
        cod, log = rodar("remontar_pdfs.py", saida, volta_zip, "--zipar", "--meta-mb", META_MB,
                         "--unidade", "decimal", "--ordem", "natural", "--so-zip")
        zips = sorted((volta_zip / "Pacotes_Agrupados").glob("*.zip"))
        maior = max((z.stat().st_size for z in zips), default=0)
        checar("remontagem termina zipada: Pacote_01.zip… dentro do teto",
               cod == 0 and bool(zips) and zips[0].name == "Pacote_01.zip" and maior <= meta,
               (f"{len(zips)} .zip, maior " + f"{maior:,}".replace(",", ".") + " bytes")
               if cod == 0 else log[-600:])
        membros, bit_a_bit, volumes = [], True, []
        for z in zips:
            with zipfile.ZipFile(z) as zf:
                for i in zf.infolist():
                    membros.append(i.filename)
                    if "_parte" in i.filename:
                        volumes.append(i.filename)
                        continue
                    bit_a_bit &= (i.compress_type == zipfile.ZIP_STORED and
                                  zf.read(i) == (origem / i.filename).read_bytes())
        checar("PDFs inteiros nos .zip da remontagem idênticos bit a bit", bit_a_bit,
               f"{len(membros) - len(volumes)} inteiros")
        grandes_div = {re.sub(r"_parte\d+de\d+", "", v) for v in volumes}
        checar("maiores que o teto divididos em volumes dentro dos .zip",
               grandes_div == {"contratos/contrato_navegavel.pdf", "obras/planta_sem_perda.pdf"},
               f"{len(volumes)} volumes de {len(grandes_div)} PDFs")
        soltos = sorted(p.relative_to(volta_zip).as_posix() for p in volta_zip.rglob("*.pdf"))
        checar("nada fica solto fora dos .zip", soltos == [], ", ".join(soltos) or "nenhum")
        volta2 = raiz / "remontados_de_novo"
        cod, log = rodar("remontar_pdfs.py", volta_zip / "Pacotes_Agrupados", volta2)
        probs = []
        for rel in ("contratos/contrato_navegavel.pdf", "obras/planta_sem_perda.pdf"):
            alvo = volta2 / rel
            probs += [f"{rel}: ausente"] if not alvo.exists() else \
                [f"{rel}: {p}" for p in comparar(origem / rel, alvo)[0]]
        checar("volumes do .zip final remontam o original", cod == 0 and not probs,
               "; ".join(probs) or "mesmas páginas, pixels, sumário e links")
    else:
        iguais = sum((volta / f.relative_to(origem)).exists() and
                     not comparar(f, volta / f.relative_to(origem))[0] for f in inteiros)
        checar("PDFs não particionados voltam com as mesmas páginas, pixels e data",
               iguais == len(inteiros), f"{iguais} de {len(inteiros)}")


def conferir_regras_zip48(origem, raiz, checar):
    """Padrões da especificação: .zip de até 48 MB (Windows), ordem pelo nº da medição."""
    print("\n== Especificação .zip 48 MB: ordem pela medição e por lista ==", flush=True)
    saida = raiz / "saida_medicao"
    cod, log = rodar("organizar_pdfs.py", origem / "medicoes", saida)      # só os padrões
    checar("agrupamento com os padrões (.zip, 48 MB, medição) sem erro", cod == 0,
           "" if cod == 0 else log[-800:])
    linhas = list(csv.DictReader(open(saida / "indice.csv", encoding="utf-8-sig"),
                                 delimiter=";")) if (saida / "indice.csv").exists() else []
    nomes = [r["documento_original"] for r in linhas]
    esperado = ["Boletim Medição 1.pdf", "2ª medição - fotos.pdf", "Relatorio_medicao_3.pdf",
                "Med-04 Pavimentacao.pdf", "BM 10 - planilha.pdf", "Anexo sem numero.pdf"]
    checar("ordem pelo número da medição, mesmo com nomes diferentes", nomes == esperado,
           " > ".join(n.split()[0][:12] for n in nomes))
    pac = sorted((saida / "Pacotes_Agrupados").glob("*.zip"))
    checar("nome do pacote no padrão Pacote_01.zip", [p.name for p in pac] == ["Pacote_01.zip"],
           ", ".join(p.name for p in pac))
    checar("teto de 48 MB do Windows (50.331.648 bytes)",
           all(p.stat().st_size <= 48 * 1024 * 1024 for p in pac))
    intactos = True
    for p in pac:
        with zipfile.ZipFile(p) as z:
            for i in z.infolist():
                intactos &= (i.compress_type == zipfile.ZIP_STORED and
                             z.read(i) == (origem / "medicoes" / i.filename).read_bytes())
    checar("PDFs dentro do .zip idênticos bit a bit aos originais", intactos)

    lista = raiz / "ordem.txt"
    lista.write_text("# ordem definida\nBM 10 - planilha.pdf\nanexo sem numero\n"
                     "medicoes/Relatorio_medicao_3.pdf\n", encoding="utf-8")
    saida2 = raiz / "saida_lista"
    cod, log = rodar("organizar_pdfs.py", origem / "medicoes", saida2, "--ordem", "lista",
                     "--lista", lista)
    linhas = list(csv.DictReader(open(saida2 / "indice.csv", encoding="utf-8-sig"),
                                 delimiter=";")) if (saida2 / "indice.csv").exists() else []
    nomes = [r["documento_original"] for r in linhas]
    checar("ordem definida em lista (não listados vão para o fim)",
           cod == 0 and nomes[:3] == ["BM 10 - planilha.pdf", "Anexo sem numero.pdf",
                                      "Relatorio_medicao_3.pdf"]
           and all(r["criterio_da_ordem"].startswith("FORA") for r in linhas[3:]),
           " > ".join(n.split()[0][:12] for n in nomes))


def conferir_ordem_criacao(origem, raiz, checar):
    """Ordem pela data de criação registrada no PDF (Info ou XMP)."""
    print("\n== Ordem por data de criação registrada no PDF ==", flush=True)
    saida = raiz / "saida_criacao"
    cod, log = rodar("organizar_pdfs.py", origem / "processos", saida, "--meta-mb", META_MB,
                     "--unidade", "decimal", "--ordem", "criacao")
    checar("agrupamento por data de criação terminou sem erro", cod == 0,
           "" if cod == 0 else log[-800:])
    linhas = list(csv.DictReader(open(saida / "indice.csv", encoding="utf-8-sig"),
                                 delimiter=";")) if (saida / "indice.csv").exists() else []
    nomes = [Path(r["documento_original"]).name for r in linhas]
    esperado = [f"processo_{n}.pdf" for n in range(15, 0, -1)]
    fontes = {r["criterio_da_ordem"].replace("data: ", "") for r in linhas}
    checar("pacotes seguem a data de criação do PDF (a mais antiga primeiro)",
           nomes == esperado, " > ".join(nomes[:4]) + " ...")
    checar("data lida do PDF (Info e XMP), não do arquivo",
           fontes <= {"registrada no PDF", "registrada no PDF (XMP)"} and len(fontes) == 2,
           ", ".join(sorted(fontes)))


def main():
    ap = argparse.ArgumentParser(description="Testa o programa neste computador.")
    ap.add_argument("--progresso", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--manter", action="store_true", help="não apaga a pasta de teste")
    args = ap.parse_args()
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    inicio = time.time()
    raiz = Path(tempfile.mkdtemp(prefix="autoteste_pdfs_"))
    origem = raiz / "originais"
    resultados = []         # (verificação, ok, detalhe)

    def checar(nome, ok, detalhe=""):
        resultados.append((nome, ok, detalhe))
        print(f"  [{'OK' if ok else 'FALHOU'}] {nome}" + (f" — {detalhe}" if detalhe else ""),
              flush=True)

    try:
        print(f"Python {sys.version.split()[0]} · pikepdf {pikepdf.__version__} · "
              f"{sys.platform}\nPasta de teste: {raiz}\n", flush=True)
        progresso(args, "teste", 0, 7, "criando PDFs de teste")
        print("Criando PDFs de teste...", flush=True)
        gerados = gerar(origem, int(META_MB * 1_000_000))
        esperado = {f: (t, f.stat().st_mtime, f.stat().st_size) for f, t in gerados.items()}
        print(f"     {len(esperado)} PDFs, {sum(f.stat().st_size for f in esperado) / 1e6:.1f} MB")
        conferir_rodada("pdf", origem, esperado, raiz, checar, args, 0)
        conferir_rodada("zip", origem, esperado, raiz, checar, args, 3)
        conferir_ordem_criacao(origem, raiz, checar)
        conferir_regras_zip48(origem, raiz, checar)
    except Exception as ex:
        import traceback
        traceback.print_exc()
        checar("execução do teste", False, f"{type(ex).__name__}: {ex}")

    falhas = [r for r in resultados if not r[1]]
    ok = not falhas
    progresso(args, "teste", 7, 7)
    print("\n================ RESULTADO ================")
    print(f"Verificações: {len(resultados)} · falhas: {len(falhas)} · "
          f"tempo: {time.time() - inicio:.0f} s")
    print("TUDO CERTO: o programa funciona neste computador." if ok else
          "HÁ FALHAS: veja as linhas marcadas com FALHOU acima.")
    if ok and not args.manter:
        shutil.rmtree(raiz, ignore_errors=True)
    else:
        print(f"Arquivos do teste mantidos em: {raiz}")
    if args.progresso:
        print("@@RESUMO|" + json.dumps(dict(ok=ok, verificacoes=len(resultados),
                                            falhas=[f"{n}: {d}" for n, _, d in falhas],
                                            pasta=str(raiz)), ensure_ascii=False), flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

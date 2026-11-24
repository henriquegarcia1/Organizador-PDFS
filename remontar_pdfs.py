#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
remontar_pdfs.py
================
Desfaz o que o organizar_pdfs.py fez: lê os pacotes (PDF único ou ZIP) e
reconstrói cada PDF original, juntando as partes na ordem (parte 1, 2, 3...).

O que faz
  1. Procura, na pasta indicada e nas subpastas, os pacotes PDF
     (Pacote_0001.pdf...), os .zip e também PDFs soltos da pasta EXCECOES.
     De cada pacote PDF, separa de volta cada documento que foi unido nele.
  2. Arquivos com nome "<documento>_parteXdeN.pdf" são agrupados por
     documento e unidos na ordem das partes, página por página, sem
     recomprimir nada.
  3. Os PDFs que não foram cortados são apenas extraídos (idênticos).
  4. Páginas gigantes que foram divididas em faixas voltam a ser uma página
     só, com a imagem reconstruída linha por linha (mesmos pixels).
  5. O sumário (marcadores) e os links internos do original são recriados
     apontando para as páginas certas do documento remontado.
  6. Os documentos são processados em ordem cronológica e cada arquivo
     recebe de volta a data de modificação do original (guardada no ZIP).
  7. Gera relatorio_remontagem.csv.
  8. Com --zipar (padrão na interface), o passo final empacota os PDFs
     restaurados em .zip de até 48 MB, com as mesmas regras da separação:
     ordem (número da medição, lista ou data), transbordo pelo tamanho real,
     nomes Pacote_01.zip..., PDFs intactos (ZIP_STORED). Os que sozinhos
     passam de 48 MB são divididos em volumes sem perda e zipados em
     sequência. Só ficam soltos os que não podem ser divididos (assinados,
     página gigante em JPEG), listados no relatório. Documento com parte faltando não é
     remontado: as partes encontradas vão para a pasta _INCOMPLETOS.

Os ZIPs nunca são alterados.

Uso:
    python remontar_pdfs.py "D:\PDFs\saida\LOTES" "D:\PDFs\remontados"
    python remontar_pdfs.py "D:\PDFs\saida" "D:\PDFs\remontados" --so-cortados
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

try:
    import pikepdf
except ImportError:
    sys.exit("Falta a biblioteca pikepdf. Instale com:\n    pip install pikepdf")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nucleo_pdf as nucleo  # noqa: E402

MIB = 1024 * 1024
PADRAO_PARTE = re.compile(r"^(?P<base>.+)_parte(?P<i>\d+)de(?P<n>\d+)(?P<dup>_\d+)?\.pdf$",
                          re.IGNORECASE)
PASTAS_IGNORADAS = {"_trabalho", "_incompletos", "_temp_remontagem", "_temp_pacotes",
                    "_trabalho_volumes"}
PASTAS_DO_ORGANIZADOR = {"excecoes", "lotes", "pacotes_agrupados"}   # removidas do caminho dos PDFs soltos


# --------------------------------------------------------------------------
# Estruturas
# --------------------------------------------------------------------------
@dataclass
class Entrada:
    """Um PDF encontrado: dentro de um ZIP ou solto numa pasta."""
    caminho_rel: str        # caminho relativo do PDF (com subpastas)
    origem: Path            # o .zip ou o próprio PDF
    membro: str | None      # nome dentro do ZIP (None = PDF solto)
    tamanho: int
    data: float             # data de modificação (timestamp)
    rotulo: str | None = None   # de onde veio, para o relatório

    @property
    def onde(self) -> str:
        if self.rotulo:
            return self.rotulo
        return f"{self.origem.name}" + (f" > {self.membro}" if self.membro else "")


@dataclass
class Documento:
    caminho_rel: str                       # caminho do PDF a ser criado
    total: int = 0                         # N de "parteXdeN" (0 = inteiro)
    partes: dict = field(default_factory=dict)   # num_parte -> Entrada
    inteiro: Entrada | None = None
    problemas: list = field(default_factory=list)

    @property
    def data(self) -> float:
        if self.inteiro:
            return self.inteiro.data
        return min(e.data for e in self.partes.values())

    @property
    def faltando(self):
        return [i for i in range(1, self.total + 1) if i not in self.partes]


def data_do_zip(info: zipfile.ZipInfo) -> float:
    try:
        return time.mktime(info.date_time + (0, 0, -1))
    except (OverflowError, ValueError):
        return time.time()


def mb(n: int) -> str:
    return f"{n / MIB:.2f}".replace(".", ",")


# --------------------------------------------------------------------------
# Leitura
# --------------------------------------------------------------------------
def procurar(entrada: Path, saida: Path):
    zips, soltos = [], []
    for raiz, pastas, arquivos in os.walk(entrada):
        raiz_p = Path(raiz)
        pastas[:] = [d for d in pastas
                     if d.lower() not in PASTAS_IGNORADAS and (raiz_p / d).resolve() != saida]
        for nome in arquivos:
            baixo = nome.lower()
            if baixo.endswith(".zip"):
                zips.append(raiz_p / nome)
            elif baixo.endswith(".pdf"):
                soltos.append(raiz_p / nome)
    chave = lambda p: str(p).lower()   # noqa: E731
    return sorted(zips, key=chave), sorted(soltos, key=chave)


def caminho_solto(pdf: Path, entrada: Path) -> str:
    """Caminho relativo de um PDF solto, sem as pastas criadas pelo organizador."""
    partes = list(pdf.relative_to(entrada).parts)
    while len(partes) > 1 and partes[0].lower() in PASTAS_DO_ORGANIZADOR:
        partes.pop(0)
    return "/".join(partes)


def registrar(docs: dict, e: Entrada, avisos: list):
    pasta = str(PurePosixPath(e.caminho_rel).parent)
    pasta = "" if pasta == "." else pasta + "/"
    nome = PurePosixPath(e.caminho_rel).name
    m = PADRAO_PARTE.match(nome)
    if m:
        rel = f"{pasta}{m['base']}{m['dup'] or ''}.pdf"
        i, n = int(m["i"]), int(m["n"])
    else:
        rel, i, n = e.caminho_rel, 0, 0
    doc = docs.setdefault(rel.lower(), Documento(rel))

    if i == 0:
        if doc.inteiro is None:
            doc.inteiro = e
        elif doc.inteiro.tamanho != e.tamanho:
            doc.problemas.append(f"duas versões diferentes ({doc.inteiro.onde} e {e.onde})")
        return
    if doc.total and doc.total != n:
        doc.problemas.append(f"partes com totais diferentes (de{doc.total} e de{n})")
        return
    if i < 1 or i > n:
        doc.problemas.append(f"número de parte inválido: {nome}")
        return
    doc.total = n
    if i in doc.partes:
        if doc.partes[i].tamanho != e.tamanho:
            doc.problemas.append(f"parte {i} aparece duas vezes com tamanhos diferentes "
                                 f"({doc.partes[i].onde} e {e.onde})")
        else:
            avisos.append(f"parte repetida ignorada: {e.onde}")
        return
    doc.partes[i] = e


def ler_tudo(zips, soltos, entrada: Path, progresso, temp_pacotes: Path):
    docs, avisos, zips_ruins = {}, [], []
    for k, z in enumerate(zips, 1):
        progresso("leitura", k - 1, len(zips), z.name)
        try:
            with zipfile.ZipFile(z) as zf:
                for info in zf.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".pdf"):
                        continue
                    nome = info.filename.replace("\\", "/").lstrip("/")
                    if ".." in PurePosixPath(nome).parts:      # caminho malicioso
                        avisos.append(f"ignorado (caminho inválido): {z.name} > {nome}")
                        continue
                    registrar(docs, Entrada(nome, z, info.filename, info.file_size,
                                            data_do_zip(info)), avisos)
        except (zipfile.BadZipFile, OSError) as ex:
            zips_ruins.append((z, str(ex)))
    progresso("leitura", len(zips), len(zips))

    # pacotes PDF: separa cada documento unido neles
    nao_pacotes, n_pacotes = [], 0
    for k, pdf in enumerate(soltos, 1):
        progresso("pacotes", k - 1, len(soltos), pdf.name)
        try:
            with pikepdf.open(pdf) as pk:
                entradas = nucleo.ler_pacote(pk)
                if entradas is None:
                    nao_pacotes.append(pdf)
                    continue
                n_pacotes += 1
                for i, e in enumerate(entradas, 1):
                    tmp = temp_pacotes / f"{pdf.stem}_{i:04d}.pdf"
                    nucleo.extrair_do_pacote(pk, e, tmp)
                    registrar(docs, Entrada(e["peca"], tmp, None, tmp.stat().st_size,
                                            float(e["data"]),
                                            f"{pdf.name} (págs. {e['pag'] + 1}-{e['pag'] + e['n']})"),
                              avisos)
        except Exception as ex:
            nao_pacotes.append(pdf)
            if not isinstance(ex, pikepdf.PasswordError):
                avisos.append(f"não foi possível ler {pdf.name}: {type(ex).__name__}: {ex}")
    progresso("pacotes", len(soltos), len(soltos))

    for pdf in nao_pacotes:
        # se há pacotes, PDFs soltos só contam se estiverem numa pasta EXCECOES
        # (evita misturar os originais quando a pasta escolhida os contém)
        if n_pacotes and not any(p.lower() == "excecoes"
                                 for p in pdf.relative_to(entrada).parts[:-1]):
            continue
        try:
            st = pdf.stat()
        except OSError as ex:
            avisos.append(f"não foi possível ler {pdf}: {ex}")
            continue
        registrar(docs, Entrada(caminho_solto(pdf, entrada), pdf, None, st.st_size,
                                st.st_mtime), avisos)
    return docs, avisos, zips_ruins, n_pacotes


# --------------------------------------------------------------------------
# Escrita
# --------------------------------------------------------------------------
def copiar_para(e: Entrada, destino: Path):
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = nucleo.nome_unico(destino.parent, "." + destino.stem, ".tmp")
    if e.membro is None:
        shutil.copyfile(e.origem, tmp)
    else:
        with zipfile.ZipFile(e.origem) as zf, zf.open(e.membro) as f, open(tmp, "wb") as d:
            shutil.copyfileobj(f, d, 1024 * 1024)
    nucleo.substituir(tmp, destino)
    os.utime(destino, (e.data, e.data))


def unir(doc: Documento, destino: Path, temp: Path):
    """
    Junta as partes 1..N em `destino`, refaz as páginas divididas em faixas
    e restaura sumário e links. Devolve (páginas, observações).
    """
    temp.mkdir(parents=True, exist_ok=True)
    arquivos, abertos = [], []
    try:
        for i in range(1, doc.total + 1):         # ordem das partes
            e = doc.partes[i]
            if e.membro is None:
                arquivos.append(e.origem)
            else:
                f = temp / f"parte_{i:04d}.pdf"
                copiar_para(e, f)
                arquivos.append(f)
        novo = pikepdf.Pdf.new()
        versao = "1.3"
        dados_doc = None
        for i, f in enumerate(arquivos):
            p = pikepdf.open(f)
            abertos.append(p)
            versao = max(versao, p.pdf_version)
            if i == 0:
                nucleo.copiar_info(p, novo)
                dados_doc = p.Root.get(nucleo.CHAVE_DOC)
            novo.pages.extend(p.pages)

        obs = []
        refeitas, problemas = nucleo.juntar_faixas(novo)
        if problemas:
            raise ValueError("; ".join(problemas))
        if refeitas:
            obs.append(f"{refeitas} página(s) gigante(s) reconstruída(s) a partir das faixas")
        links, marcadores = nucleo.restaurar_navegacao(novo, dados_doc)
        if marcadores:
            obs.append(f"sumário restaurado ({marcadores} marcador(es) de 1º nível)")
        if links:
            obs.append(f"{links} link(s) interno(s) restaurado(s)")
        paginas = len(novo.pages)
        if dados_doc is not None and "/PagTotal" in dados_doc \
                and int(dados_doc.PagTotal) != paginas:
            raise ValueError(f"o original tinha {int(dados_doc.PagTotal)} páginas e a "
                             f"remontagem ficou com {paginas}")
        destino.parent.mkdir(parents=True, exist_ok=True)
        tmp = nucleo.nome_unico(destino.parent, "." + destino.stem, ".tmp")
        novo.save(tmp, min_version=versao)
        novo.close()
        nucleo.substituir(tmp, destino)
        os.utime(destino, (doc.data, doc.data))
        return paginas, obs
    finally:
        for p in abertos:
            p.close()
        shutil.rmtree(temp, ignore_errors=True)


# --------------------------------------------------------------------------
# Programa principal
# --------------------------------------------------------------------------
def zipar_resultado(restaurados, saida: Path, args, progresso, relatorio):
    """
    Passo final: empacota os PDFs restaurados em .zip de até --meta-mb, com
    as mesmas regras da separação (ordem, transbordo real, ZIP_STORED,
    Pacote_01.zip...). Os que sozinhos passam do teto são divididos em
    volumes sem perda (regra 1) e entram nos .zip em sequência, a parte 1
    antes da 2 (a menos que --nao-dividir). Devolve um resumo.
    """
    import re
    import organizar_pdfs as org
    org.UNIDADE = org.MIB if args.unidade == "windows" else 1_000_000
    meta = int(round(args.meta_mb * org.UNIDADE))
    capacidade = meta - org.RESERVA_FIXA_ZIP
    lista = org.ler_lista_ordem(Path(args.lista).expanduser()) if args.ordem == "lista" else {}
    print(f"\nZipando {len(restaurados)} PDF(s) em pacotes de até {args.meta_mb:g} MB "
          f"(ordem: {org.ORDENS[args.ordem]})...", flush=True)
    ordem_info = org.calcular_ordem(restaurados, args.ordem, lista)
    restaurados = sorted(restaurados, key=lambda r: ordem_info[r[1]][0])

    pasta_vol = saida / "_trabalho_volumes"
    pecas, fora, divididos = [], [], []     # divididos: (original, caminho, nº de volumes)

    def deixar_fora(rel, tam, motivo):
        fora.append(rel)
        relatorio.append([rel, "", "", "", mb(tam), f"NÃO ZIPADO: {motivo} (ficou solto na pasta)", ""])
        print(f"   ! fora dos .zip: {rel} — {motivo}")

    grandes = [r for r in restaurados if org.custo_no_zip(r[1], r[2]) > capacidade]
    for k, (caminho, rel, tam, mtime) in enumerate(restaurados):
        chave_doc = ordem_info[rel][0]
        if org.custo_no_zip(rel, tam) <= capacidade:
            pecas.append(org.Peca(caminho, rel, rel, 0, "", "", tam, "", False, mtime,
                                  chave=(chave_doc, 0)))
            continue
        if args.nao_dividir:
            deixar_fora(rel, tam, f"sozinho passa do teto de {args.meta_mb:g} MB")
            continue
        # regra 1: divide em volumes sem perda, cada um cabendo num .zip
        progresso("volumes", len(divididos) + len(fora), len(grandes), rel)
        print(f"   dividindo em volumes (maior que o teto): {rel}  ({tam / org.UNIDADE:.1f} MB)",
              flush=True)
        sub = PurePosixPath(rel).parent.as_posix()
        sub = "" if sub == "." else sub
        alvo = capacidade - org.custo_no_zip(rel + "_parte000de000", 0)
        try:
            vols = org.cortar_pdf(caminho, alvo, pasta_vol / sub, PurePosixPath(rel).stem,
                                  args.cortar_assinados, avisar=lambda t: print(t, flush=True))
        except org.Assinado:
            deixar_fora(rel, tam, "PDF com assinatura digital acima do teto; dividir invalidaria "
                                  "a assinatura (use a opção de dividir também os assinados)")
            continue
        except Exception as ex:
            deixar_fora(rel, tam, f"não foi possível dividir: {type(ex).__name__}: {ex}")
            continue
        if any(v["excede"] for v in vols):
            for v in vols:
                nucleo.apagar(v["arquivo"])
            motivo = next(v["motivo"] for v in vols if v["excede"])
            deixar_fora(rel, tam, f"tem página que sozinha passa do teto e não pode ser "
                                  f"dividida sem perda ({motivo})")
            continue
        for v in vols:
            nome = (f"{sub}/" if sub else "") + v["arquivo"].name
            pag = f'{v["pag_ini"]} (faixa {v["faixa"]})' if v.get("faixa") \
                else f'{v["pag_ini"]}-{v["pag_fim"]}'
            pecas.append(org.Peca(v["arquivo"], nome, rel, v["num"], f'{v["num"]}/{v["de"]}',
                                  pag, v["tamanho"], "", True, mtime,
                                  chave=(chave_doc, v["num"])))
        divididos.append((rel, caminho, len(vols)))
        relatorio.append([rel, "", "", "", mb(tam),
                          f"ZIPADO EM {len(vols)} VOLUMES (inteiro passa do teto de "
                          f"{args.meta_mb:g} MB; dividido sem perda)", ""])
    if grandes:
        progresso("volumes", len(grandes), len(grandes))

    pecas.sort(key=lambda p: p.chave)
    pasta = saida / org.PASTA_PACOTES
    pasta.mkdir(parents=True, exist_ok=True)
    for antigo in pasta.iterdir():
        if re.fullmatch(r"_?Pacote_\d+\.zip(\.tmp)?", antigo.name) or antigo.name.endswith(".tmp"):
            nucleo.apagar(antigo)
    pacotes = org.empacotar_zip(pecas, meta, capacidade, pasta, False,
                                lambda e, a, t, x="": progresso("zip", a, t, x),
                                lambda t: print(t, flush=True)) if pecas else []
    org.nomear_pacotes(pacotes, "zip")

    linhas, estouros, maior = [], [], 0
    for pk in pacotes:
        maior = max(maior, pk["tamanho"])
        if pk["tamanho"] > meta:
            estouros.append(pk["nome"])
        for p in pk["itens"]:
            linhas.append([pk["nome"], p.nome_zip, p.original, p.parte, p.paginas,
                           *ordem_info[p.original][1:], p.tamanho, org.mb(p.tamanho),
                           org.mb(pk["tamanho"])])
    nucleo.gravar_csv(saida / "indice_zip.csv",
                      ["pacote", "arquivo", "documento", "parte", "paginas", "valor_da_ordem",
                       "criterio_da_ordem", "tamanho_bytes", "tamanho_MB",
                       "tamanho_do_pacote_MB"], linhas)

    integros = all(zipfile.ZipFile(pk["caminho"]).testzip() is None for pk in pacotes)
    if args.so_zip:
        # só apaga depois de conferir que cada .zip está íntegro
        if integros:
            for pk in pacotes:
                for p in pk["itens"]:
                    if not p.temporario:
                        nucleo.apagar(p.caminho)
            for _, caminho, _ in divididos:          # o original inteiro já está nos volumes
                nucleo.apagar(caminho)
            for raiz, pastas, arquivos in os.walk(saida, topdown=False):
                if raiz != str(saida) and not os.listdir(raiz):
                    os.rmdir(raiz)
        else:
            print("   ! algum .zip falhou na conferência: os PDFs soltos foram mantidos")
    shutil.rmtree(pasta_vol, ignore_errors=True)
    return dict(zips=len(pacotes), fora=len(fora), maior=maior, estouros=estouros,
                divididos=len(divididos), volumes=sum(n for _, _, n in divididos),
                unidade=org.UNIDADE, pasta=str(pasta))


def main():
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Reconstrói os PDFs originais a partir dos lotes ZIP.")
    ap.add_argument("entrada", help="pasta com os lotes .zip (procura também nas subpastas)")
    ap.add_argument("saida", help="pasta onde os PDFs remontados serão gravados")
    ap.add_argument("--so-cortados", action="store_true",
                    help="grava só os PDFs que estavam em partes (não extrai os inteiros)")
    ap.add_argument("--zipar", action="store_true",
                    help="no fim, empacota os PDFs restaurados em .zip de até --meta-mb")
    ap.add_argument("--meta-mb", type=float, default=48.0, help="teto de cada .zip (padrão 48)")
    ap.add_argument("--unidade", choices=["windows", "decimal"], default="windows",
                    help="windows: 1 MB = 1.048.576 bytes (padrão); decimal: 1.000.000")
    ap.add_argument("--ordem", default="medicao",
                    choices=["medicao", "lista", "natural", "criacao", "criacao-arquivo", "data"],
                    help="ordem dos PDFs nos .zip (padrão: número da medição)")
    ap.add_argument("--lista", help="arquivo .txt com a ordem definida (com --ordem lista)")
    ap.add_argument("--nao-dividir", action="store_true",
                    help="não divide em volumes os PDFs maiores que o teto (ficam soltos)")
    ap.add_argument("--cortar-assinados", action="store_true",
                    help="divide também PDFs com assinatura digital (a assinatura deixa de valer)")
    ap.add_argument("--so-zip", action="store_true",
                    help="depois de zipar, apaga os PDFs soltos que foram para os .zip")
    ap.add_argument("--progresso", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.ordem == "lista" and args.zipar and not args.lista:
        ap.error("--ordem lista precisa de --lista arquivo.txt")

    def progresso(etapa, atual, total, texto=""):
        if args.progresso:
            print(f"@@PROGRESSO|{etapa}|{atual}|{total}|{texto}", flush=True)

    entrada = Path(args.entrada).expanduser().resolve()
    saida = Path(args.saida).expanduser().resolve()
    if not entrada.is_dir():
        sys.exit(f"Pasta de entrada não encontrada: {entrada}")
    if saida == entrada:
        sys.exit("A pasta de saída precisa ser diferente da de entrada.")
    saida.mkdir(parents=True, exist_ok=True)
    inicio = time.time()

    zips, soltos = procurar(entrada, saida)
    if not zips and not soltos:
        sys.exit("Nenhum .zip ou .pdf encontrado na pasta de entrada.")
    if zips:
        print(f"Pacotes ZIP           : {len(zips)}")
    if soltos:
        print(f"Arquivos PDF          : {len(soltos)}")
    print("Lendo o conteúdo dos pacotes...", flush=True)
    temp_pacotes = saida / "_temp_pacotes"
    docs, avisos, zips_ruins, n_pacotes = ler_tudo(zips, soltos, entrada, progresso,
                                                   temp_pacotes)
    if n_pacotes:
        print(f"Pacotes PDF lidos     : {n_pacotes}")

    # ordem cronológica (data do original), depois caminho
    fila = sorted(docs.values(), key=lambda d: (d.data, d.caminho_rel.lower()))
    if args.so_cortados:
        fila = [d for d in fila if d.total]
    em_partes = sum(1 for d in fila if d.total)
    print(f"Documentos            : {len(fila)}  ({em_partes} em partes)")

    pasta_inc = saida / "_INCOMPLETOS"
    temp = saida / "_temp_remontagem"
    relatorio = []
    restaurados = []          # (caminho, rel, tamanho, mtime_ns) dos PDFs gravados
    cont = dict(remontados=0, extraidos=0, incompletos=0, erros=0)

    for k, doc in enumerate(fila, 1):
        progresso("remontagem", k - 1, len(fila), doc.caminho_rel)
        destino = saida / doc.caminho_rel
        data_txt = datetime.fromtimestamp(doc.data).strftime("%d/%m/%Y %H:%M:%S")
        partes_txt = f"{len(doc.partes)}/{doc.total}" if doc.total else ""
        locais = " | ".join(f"{i}: {doc.partes[i].onde}" for i in sorted(doc.partes))

        if doc.total and doc.inteiro is not None:
            doc.problemas.append(f"existe também um arquivo inteiro com o mesmo nome "
                                 f"({doc.inteiro.onde})")
        if doc.problemas:
            cont["erros"] += 1
            relatorio.append([doc.caminho_rel, data_txt, partes_txt, "", "",
                              "CONFLITO: " + "; ".join(doc.problemas), locais])
            print(f"[{k}/{len(fila)}] ! conflito: {doc.caminho_rel}")
            continue

        try:
            if not doc.total:                                   # PDF inteiro
                copiar_para(doc.inteiro, destino)
                st = destino.stat()
                restaurados.append((destino, doc.caminho_rel, st.st_size, st.st_mtime_ns))
                cont["extraidos"] += 1
                relatorio.append([doc.caminho_rel, data_txt, "", "", mb(doc.inteiro.tamanho),
                                  "EXTRAÍDO (não estava cortado)", doc.inteiro.onde])
                continue

            falta = doc.faltando
            if falta:                                           # parte faltando
                for i, e in sorted(doc.partes.items()):
                    nome = PurePosixPath(e.membro or e.origem.name).name
                    copiar_para(e, pasta_inc / PurePosixPath(doc.caminho_rel).parent / nome)
                cont["incompletos"] += 1
                relatorio.append([doc.caminho_rel, data_txt, partes_txt, "", "",
                                  "INCOMPLETO: falta(m) a(s) parte(s) "
                                  + ", ".join(map(str, falta)), locais])
                print(f"[{k}/{len(fila)}] ! incompleto (faltam {falta}): {doc.caminho_rel}")
                continue

            print(f"[{k}/{len(fila)}] remontando {doc.caminho_rel}  ({doc.total} partes)",
                  flush=True)
            paginas, obs = unir(doc, destino, temp)
            st = destino.stat()
            restaurados.append((destino, doc.caminho_rel, st.st_size, st.st_mtime_ns))
            cont["remontados"] += 1
            relatorio.append([doc.caminho_rel, data_txt, partes_txt, paginas,
                              mb(destino.stat().st_size),
                              "REMONTADO" + (": " + "; ".join(obs) if obs else ""), locais])
            for o in obs:
                print(f"      {o}")
        except Exception as ex:
            cont["erros"] += 1
            relatorio.append([doc.caminho_rel, data_txt, partes_txt, "", "",
                              f"ERRO: {type(ex).__name__}: {ex}", locais])
            print(f"[{k}/{len(fila)}] ! erro em {doc.caminho_rel}: {type(ex).__name__}: {ex}")
    progresso("remontagem", len(fila), len(fila))
    shutil.rmtree(temp, ignore_errors=True)
    shutil.rmtree(temp_pacotes, ignore_errors=True)

    resumo_zip = None
    if args.zipar and restaurados:
        resumo_zip = zipar_resultado(restaurados, saida, args, progresso, relatorio)

    for z, erro in zips_ruins:
        cont["erros"] += 1
        relatorio.append(["", "", "", "", "", f"ZIP ILEGÍVEL: {erro}", str(z)])
    for a in avisos:
        relatorio.append(["", "", "", "", "", "AVISO: " + a, ""])

    nucleo.gravar_csv(saida / "relatorio_remontagem.csv",
                      ["documento", "data_modificacao", "partes", "paginas", "tamanho_MB",
                       "situacao", "onde_estava"], relatorio)

    print("\n================ RESUMO ================")
    print(f"Documentos processados : {len(fila)}  (em ordem cronológica)")
    print(f"Remontados             : {cont['remontados']}")
    print(f"Extraídos inteiros     : {cont['extraidos']}")
    print(f"Incompletos            : {cont['incompletos']}" +
          ("  -> partes em _INCOMPLETOS" if cont["incompletos"] else ""))
    print(f"Conflitos/erros        : {cont['erros']}")
    print(f"Tempo                  : {(time.time() - inicio) / 60:.1f} min")
    print(f"Saída                  : {saida}")
    if resumo_zip:
        print(f"Pacotes .zip           : {resumo_zip['zips']}  (maior "
              f"{resumo_zip['maior'] / resumo_zip['unidade']:.2f} MB, teto {args.meta_mb:g} MB) "
              f"em {resumo_zip['pasta']}")
        if resumo_zip["divididos"]:
            print(f"Divididos em volumes   : {resumo_zip['divididos']}  (maiores que o teto; "
                  f"{resumo_zip['volumes']} volumes dentro dos .zip)")
        if resumo_zip["fora"]:
            print(f"Fora dos .zip          : {resumo_zip['fora']}  (não puderam ser divididos; "
                  "ficaram soltos na pasta, veja o relatório)")
    print("Detalhes em relatorio_remontagem.csv" + (" e indice_zip.csv" if resumo_zip else ""))
    if args.progresso:
        extra = {}
        if resumo_zip:
            extra = dict(zips=resumo_zip["zips"], fora_zip=resumo_zip["fora"],
                         divididos=resumo_zip["divididos"], volumes=resumo_zip["volumes"],
                         maior_zip_mb=round(resumo_zip["maior"] / resumo_zip["unidade"], 2),
                         meta_mb=args.meta_mb, estouros=resumo_zip["estouros"],
                         pasta_zip=resumo_zip["pasta"])
        print("@@RESUMO|" + json.dumps(dict(cont, documentos=len(fila), saida=str(saida),
                                            minutos=round((time.time() - inicio) / 60, 1),
                                            **extra),
                                       ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

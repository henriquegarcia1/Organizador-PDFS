#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
organizar_pdfs.py
=================
Prepara uma pasta de PDFs para envio com limite de tamanho por arquivo.

REGRA 1 – Partição
  PDFs maiores que o teto são particionados por páginas em volumes
  "cheios + resto", SEM PERDA (DPI nativo, nada é recomprimido). Página que
  sozinha passa do teto: gravação mais compacta sem perda e, se preciso,
  divisão da imagem em faixas sem perda (remontar_pdfs.py junta de volta).

REGRA 2 – Agrupamento
  * Teto de segurança: 47,5 MB por pacote (margem abaixo de 48 MB).
  * Qualidade intacta: cada pacote é um PDF único com as páginas unidas
    diretamente, sem compressão (opção --formato zip: pacotes ZIP).
  * Ordenação natural: 15 antes de 20, Parte_1 antes de Parte_2
    (opção --ordem data: data de modificação).
  * Agrupamento sequencial: soma em ordem; quando o próximo não cabe, o
    pacote fecha e um novo começa. O tamanho é medido no arquivo gravado.
  * Trava de excesso: arquivo que sozinho passa do teto é ignorado e
    avisado (excecoes.csv) antes de agrupar.
  * Isolamento: pacotes na subpasta Pacotes_Agrupados; os originais não são
    alterados nem copiados.

Cada pacote PDF tem um marcador por documento e guarda os dados para que
remontar_pdfs.py separe os documentos e reconstrua os originais.

Unidade: por padrão 1 MB = 1.000.000 bytes, então 47,5 MB = 47.500.000 bytes,
abaixo de 48 MB em qualquer forma de contar. Com --mb-windows,
1 MB = 1.048.576 bytes (como o Windows mostra).

Uso:
    python organizar_pdfs.py "C:\PDFs"                      (saída em C:\PDFs\Pacotes_Agrupados)
    python organizar_pdfs.py "C:\PDFs" "D:\Envio" --meta-mb 47.5 --ordem natural
"""

import argparse
import json
import math
import os
import re
import shutil
import sys
import time
import unicodedata
import zipfile
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pikepdf
except ImportError:
    sys.exit("Falta a biblioteca pikepdf. Instale com:\n    pip install pikepdf")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nucleo_pdf as nucleo  # noqa: E402

MIB = 1024 * 1024
UNIDADE = 1_000_000           # bytes por MB (troca para MIB com --mb-windows)
PASTA_PACOTES = "Pacotes_Agrupados"
RESERVA_FIXA_ZIP = 4 * 1024   # rodapé do ZIP + folga
RESERVA_PACOTE = 128 * 1024   # marcador por documento + dados de remontagem no pacote PDF
CHEIA_O_BASTANTE = 0.97       # parte com 97% do alvo já é considerada cheia
VERSAO_CORTE = 2              # muda quando a forma de cortar muda (invalida o cache)


# --------------------------------------------------------------------------
# Estruturas
# --------------------------------------------------------------------------
@dataclass
class Peca:
    """Um arquivo que vai para um pacote (PDF inteiro ou parte)."""
    caminho: Path      # onde o arquivo está no disco
    nome_zip: str      # nome da peça (mantém as subpastas)
    original: str      # documento de origem (caminho relativo)
    num_parte: int     # 0 = arquivo inteiro
    parte: str         # "" ou "2/3"
    paginas: str       # "" ou "121-260"
    tamanho: int       # bytes
    grupo: str         # subpasta de 1º nível (usada com --por-pasta)
    temporario: bool   # True = parte gerada pelo script
    data_ns: int       # data de modificação do documento original
    obs: str = ""      # observação para o índice
    pag_orig: int = 0  # 1ª página desta peça no original (0 = 1ª)
    faixa: bool = False
    chave: tuple = field(default=(), repr=False)   # ordem de agrupamento

    @property
    def custo(self) -> int:
        return custo_no_zip(self.nome_zip, self.tamanho)


def custo_no_zip(nome_zip: str, tamanho: int) -> int:
    """Bytes que a peça ocupa num ZIP sem compressão (dados + cabeçalhos)."""
    n = len(nome_zip.encode("utf-8"))
    return tamanho + 2 * n + 30 + 46 + 64


def chave_natural(texto: str) -> tuple:
    """Ordem de leitura humana: 'Parte_2' antes de 'Parte_10', sem diferenciar acento/caixa."""
    t = unicodedata.normalize("NFKD", texto.casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                 for p in re.split(r"(\d+)", t) if p)


def chave_caminho(rel: str) -> tuple:
    return tuple(chave_natural(c) for c in rel.split("/"))


def data_zip(data_ns: int):
    """Data de modificação no formato que o ZIP guarda (mínimo 1980)."""
    t = time.localtime(data_ns / 1e9)
    return max(tuple(t[:6]), (1980, 1, 1, 0, 0, 0))


ORDENS = {
    "medicao": "número da medição",
    "lista": "ordem definida em lista",
    "natural": "natural pelo nome",
    "data": "data de modificação",
    "criacao": "data de criação registrada no PDF",
    "criacao-arquivo": "data de criação do arquivo",
}

_RE_DATA_PDF = re.compile(r"D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
                          r"(?:([Zz+\-])(\d{2})?'?(\d{2})?'?)?")


def ler_data_pdf(texto: str):
    """Converte uma data no formato PDF (D:20240315103000-03'00') em timestamp."""
    from datetime import timedelta, timezone
    m = _RE_DATA_PDF.match(texto.strip())
    if not m:
        return None
    ano, mes, dia, hora, mi, se = (int(m.group(i) or d) for i, d in
                                   zip(range(1, 7), (0, 1, 1, 0, 0, 0)))
    try:
        if m.group(7) in ("Z", "z"):
            fuso = timezone.utc
        elif m.group(7) in ("+", "-"):
            delta = timedelta(hours=int(m.group(8) or 0), minutes=int(m.group(9) or 0))
            fuso = timezone(delta if m.group(7) == "+" else -delta)
        else:
            fuso = None
        dt = datetime(ano, mes, dia, hora, mi, se, tzinfo=fuso)
        return dt.timestamp()
    except (ValueError, OverflowError):
        return None


def normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto.casefold().replace("°", "o"))
    return "".join(c for c in t if not unicodedata.combining(c))


# grafias aceitas para "medição" (já sem acento): medição, mediçao, mediação
# (erro comum de digitação), medições
_PALAVRA_MED = r"(?:mediacao|medicao|medicoes|mediacoes)"
# "medição 15", "Medicao_15", "Med-15", "BM 15", "medição nº 15"
_RE_MED_DEPOIS = re.compile(r"(?<![a-z0-9])(?:" + _PALAVRA_MED[3:-1] + r"|med|bm)"
                            r"[\s._\-]*(?:n[o.]?[\s._\-]*)?(\d+)")
# "15ª medição", "15a medicao", "15 - medicao", "1ª MEDIAÇÃO"
_RE_MED_ANTES = re.compile(r"(?<![0-9])(\d+)[\s._\-]*(?:a|o)?[\s._\-]*" + _PALAVRA_MED)


def numero_medicao(rel: str):
    """Número da medição no nome do arquivo (ou, se não houver, no nome das pastas)."""
    partes = rel.split("/")
    for nome in [Path(partes[-1]).stem] + list(reversed(partes[:-1])):
        t = normalizar(nome)
        m = _RE_MED_DEPOIS.search(t) or _RE_MED_ANTES.search(t)
        if m:
            return int(m.group(1))
    return None


def ler_lista_ordem(arquivo: Path) -> dict:
    """
    Lê a ordem definida: um arquivo de texto com um nome por linha (só o nome
    do arquivo ou o caminho relativo; ".pdf" opcional; linhas com # são
    comentário). Devolve {nome normalizado: posição}.
    """
    try:
        texto = arquivo.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        texto = arquivo.read_text(encoding="latin-1")
    pos = {}
    for linha in texto.splitlines():
        linha = linha.strip().strip('"')
        if not linha or linha.startswith("#"):
            continue
        n = len(set(pos.values()))
        chave = _chave_lista(linha)
        if chave in pos:
            continue
        pos[chave] = n
        # também pelo nome do arquivo sozinho (aceita caminho completo, C:\...\x.pdf)
        pos.setdefault(chave.rsplit("/", 1)[-1], n)
    return pos


def _chave_lista(nome: str) -> str:
    t = normalizar(nome.replace("\\", "/")).strip("/")
    return t[:-4] if t.endswith(".pdf") else t


def posicao_na_lista(rel: str, lista: dict):
    for chave in (_chave_lista(rel), _chave_lista(rel.split("/")[-1])):
        if chave in lista:
            return lista[chave]
    return None


def data_criacao_arquivo(st):
    """Data de criação do arquivo (Windows/macOS); onde não existe, a de modificação."""
    t = getattr(st, "st_birthtime", None)
    if t:
        return t, "criação do arquivo"
    if os.name == "nt":
        return st.st_ctime, "criação do arquivo"
    return st.st_mtime, "modificação (sistema sem data de criação)"


def data_criacao_pdf(caminho: Path, st):
    """
    Data de criação registrada dentro do PDF (Info /CreationDate ou XMP
    xmp:CreateDate). Se não houver, usa a data de criação do arquivo.
    """
    try:
        with pikepdf.open(caminho) as pdf:
            bruto = pdf.docinfo.get("/CreationDate")
            if bruto is not None:
                t = ler_data_pdf(str(bruto))
                if t is not None:
                    return t, "registrada no PDF"
            try:
                with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                    iso = meta.get("xmp:CreateDate")
                if iso:
                    iso = str(iso).replace("Z", "+00:00")
                    return datetime.fromisoformat(iso).timestamp(), "registrada no PDF (XMP)"
            except Exception:
                pass
    except Exception:
        pass
    return data_criacao_arquivo(st)


def _fmt_data(t):
    try:
        return datetime.fromtimestamp(t).strftime("%d/%m/%Y %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def calcular_ordem(info, ordem: str, lista: dict = None, estats: dict = None,
                   progresso=lambda *a: None) -> dict:
    """
    Critério de ordem de cada arquivo. `info` é uma lista de
    (caminho, rel, tamanho, mtime_ns). Devolve rel -> (chave, valor, critério).
    Usado na separação (aba 1) e no .zip final da remontagem (aba 2).
    """
    lista = lista or {}
    estats = estats or {}
    ordem_info = {}
    # maior número de medição de cada pasta: um arquivo sem número (nota de
    # empenho, anexo...) fica logo depois da última medição da própria pasta
    numeros, maior_da_pasta = {}, {}
    if ordem == "medicao":
        for _, rel, _, _ in info:
            numeros[rel] = numero_medicao(rel)
            pasta = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if numeros[rel] is not None:
                maior_da_pasta[pasta] = max(maior_da_pasta.get(pasta, 0), numeros[rel])
    for k, (p, rel, _, mtime) in enumerate(info, 1):
        nat = chave_caminho(rel)
        st = estats.get(rel) or Path(p).stat()
        if ordem == "natural":
            ordem_info[rel] = (nat, "", "nome (ordem natural)")
        elif ordem == "lista":
            pos = posicao_na_lista(rel, lista)
            if pos is None:
                ordem_info[rel] = ((1, 0, nat), "", "FORA DA LISTA (vai para o fim)")
            else:
                ordem_info[rel] = ((0, pos, nat), str(pos + 1), "posição na lista")
        elif ordem == "medicao":
            num = numeros[rel]
            pasta = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if num is not None:
                ordem_info[rel] = ((0, num, 0.0, nat), f"medição {num}", "número da medição no nome")
            elif pasta in maior_da_pasta:
                ref = maior_da_pasta[pasta]
                t, fonte = data_criacao_pdf(p, st)
                ordem_info[rel] = ((0, ref + 0.5, t, nat), f"após a medição {ref}",
                                   f"SEM NÚMERO DE MEDIÇÃO: logo após a última medição da "
                                   f"pasta {pasta or '(raiz)'}")
            else:
                t, fonte = data_criacao_pdf(p, st)
                ordem_info[rel] = ((1, 0, t, nat), _fmt_data(t),
                                   f"SEM NÚMERO DE MEDIÇÃO (vai para o fim); data: {fonte}")
        else:
            if ordem == "data":
                t, fonte = mtime / 1e9, "modificação"
            elif ordem == "criacao-arquivo":
                t, fonte = data_criacao_arquivo(st)
            else:
                t, fonte = data_criacao_pdf(p, st)
            ordem_info[rel] = ((t, nat), _fmt_data(t), f"data: {fonte}")
        if k % 200 == 0:
            progresso("datas", k, len(info))
    return ordem_info


def data_texto(data_ns: int) -> str:
    return datetime.fromtimestamp(data_ns / 1e9).strftime("%d/%m/%Y %H:%M:%S")


def mb(n: int) -> str:
    """Bytes -> MB (na unidade escolhida) com vírgula decimal, para o Excel em pt-BR."""
    return f"{n / UNIDADE:.2f}".replace(".", ",")


# --------------------------------------------------------------------------
# Corte de PDF
# --------------------------------------------------------------------------
class Assinado(Exception):
    """PDF com assinatura digital que precisaria ser cortado."""


def gravar_trecho(src, idx, ini: int, k: int, destino: Path, versao: str,
                  otimizar: bool = False) -> int:
    """Grava as páginas [ini, ini+k) de src em destino. Devolve o tamanho."""
    dst = pikepdf.Pdf.new()
    try:
        dst.pages.extend(src.pages[ini:ini + k])
        nucleo.copiar_info(src, dst)           # título, autor etc. do original
        nucleo.marcar_links(src, idx, dst, ini, k)
        # Sem isto, PDFs cujas páginas compartilham a mesma lista de recursos
        # levariam TODAS as imagens do original para dentro de cada parte.
        dst.remove_unreferenced_resources()
        if otimizar:
            nucleo.gravar_otimizado(dst, destino, versao)
        else:
            dst.save(destino, min_version=versao)
    finally:
        dst.close()
    return destino.stat().st_size


def glob_escape(texto: str) -> str:
    """Escapa [ ] * ? para usar um nome de arquivo dentro de um padrão glob."""
    return re.sub(r"([\[\]*?])", r"[\1]", texto)


def cortar_pdf(origem: Path, alvo: int, pasta: Path, base: str,
               cortar_assinados: bool = False, avisar=print):
    """
    Corta `origem` em partes de até `alvo` bytes, no esquema cheio + resto.

    O tamanho de uma parte só é conhecido depois de gravada, então para cada
    parte o script procura o maior número de páginas que ainda cabe: começa
    por uma estimativa e ajusta medindo o arquivo real.

    Página que sozinha passa do alvo: 1) tenta gravá-la de forma mais
    compacta, sem perda; 2) se não bastar, divide a imagem principal em
    faixas (uma por arquivo), sem perda; 3) se nada disso for possível, ela
    vai para as exceções.

    Se o PDF tem sumário ou links internos, uma 2ª passagem completa cada
    parte: sumário inteiro e links apontando para a parte certa.

    Devolve lista de dicts: arquivo, num, de, pag_ini, pag_fim, tamanho,
    excede, motivo, faixa, assinado.
    """
    pasta.mkdir(parents=True, exist_ok=True)
    provisorias = []
    # cada tentativa grava num arquivo NOVO: nunca se sobrescreve nem se renomeia
    # por cima de um arquivo que o antivírus/OneDrive possa estar segurando
    def novo_teste():
        return nucleo.nome_unico(pasta, f"{base}.__t__")

    def guardar(arquivo):
        prov = pasta / f"{base}.__p{len(provisorias) + 1}__.pdf"
        nucleo.substituir(arquivo, prov)
        return prov

    with pikepdf.open(origem) as src:
        assinado = nucleo.esta_assinado(src)
        if assinado and not cortar_assinados:
            raise Assinado()
        total = len(src.pages)
        versao = src.pdf_version
        idx = nucleo.indice_paginas(src)
        marcadores = nucleo.ler_marcadores(src, idx)
        navegacao = bool(marcadores) or nucleo.tem_links_internos(src)
        # reserva espaço para o sumário e os links que entram na 2ª passagem
        margem = (3 * len(json.dumps(marcadores).encode()) + 64 * 1024) if navegacao else 0
        alvo_ef = alvo - margem
        media = origem.stat().st_size / max(total, 1)
        ini = 0
        while ini < total:
            restantes = total - ini
            cabe, cabe_tam = 0, 0          # maior qtde de páginas que coube
            nao_cabe = restantes + 1       # menor qtde que não coube
            melhor = falho = None          # arquivos da melhor tentativa e da última que não coube
            k = max(1, min(restantes, int(alvo_ef / media)))
            tam = 0
            tentativa = 0
            while True:
                tentativa += 1
                teste = novo_teste()
                tam = gravar_trecho(src, idx, ini, k, teste, versao)
                if tam <= alvo_ef:
                    cabe, cabe_tam = k, tam
                    if melhor:
                        nucleo.apagar(melhor)
                    melhor = teste
                    if k == restantes or tam >= CHEIA_O_BASTANTE * alvo_ef:
                        break
                else:
                    nao_cabe = k
                    if falho:
                        nucleo.apagar(falho)
                    falho = teste
                if nao_cabe - cabe <= 1:
                    break
                # próximo palpite: proporcional nas primeiras tentativas,
                # depois busca binária
                if tentativa <= 4:
                    if cabe:
                        prox = cabe + int((alvo_ef - cabe_tam) / (cabe_tam / cabe))
                    else:
                        prox = int(k * alvo_ef / tam * 0.98)
                else:
                    prox = (cabe + nao_cabe) // 2
                if not (cabe < prox < nao_cabe):
                    prox = (cabe + nao_cabe) // 2
                k = prox

            if cabe == 0:
                # a última tentativa foi com 1 página e ela sozinha não coube
                g = ini
                if falho:
                    nucleo.apagar(falho)
                avisar(f"      página {g + 1} sozinha tem {tam / UNIDADE:.1f} MB: "
                       "tentando gravar de forma mais compacta, sem perda...")
                otim = novo_teste()
                tam_otim = gravar_trecho(src, idx, g, 1, otim, versao, otimizar=True)
                if tam_otim <= alvo_ef:
                    melhor, cabe, cabe_tam = otim, 1, tam_otim
                    avisar(f"      ok: {tam_otim / UNIDADE:.1f} MB")
                else:
                    avisar(f"      ainda {tam_otim / UNIDADE:.1f} MB: dividindo a página em faixas, sem perda...")
                    try:
                        faixas = nucleo.dividir_em_faixas(src, idx, g, alvo_ef, pasta, base,
                                                          versao, tam_otim)
                    except nucleo.NaoDivisivel as ex:
                        avisar(f"      ! não foi possível: {ex}")
                        provisorias.append(dict(arquivo=guardar(otim), pag_ini=g + 1,
                                                pag_fim=g + 1, tamanho=tam_otim, excede=True,
                                                motivo=str(ex), faixa=""))
                        ini += 1
                        continue
                    nucleo.apagar(otim)
                    avisar(f"      ok: {len(faixas)} faixas de até "
                           f"{max(t for _, t in faixas) / UNIDADE:.1f} MB")
                    for i, (arq, t) in enumerate(faixas, 1):
                        provisorias.append(dict(arquivo=guardar(arq), pag_ini=g + 1,
                                                pag_fim=g + 1, tamanho=t, excede=False,
                                                motivo="", faixa=f"{i}/{len(faixas)}"))
                    ini += 1
                    continue
            elif falho:
                nucleo.apagar(falho)

            provisorias.append(dict(arquivo=guardar(melhor), pag_ini=ini + 1,
                                    pag_fim=ini + cabe, tamanho=cabe_tam, excede=False,
                                    motivo="", faixa=""))
            ini += cabe

    for resto in pasta.glob(f"{glob_escape(base)}.__t__.*.pdf"):   # tentativas que sobraram
        nucleo.apagar(resto, tentativas=3)

    # agora que o total de partes é conhecido, dá os nomes definitivos
    n = len(provisorias)
    largura = len(str(n))
    for i, p in enumerate(provisorias, 1):
        final = pasta / f"{base}_parte{i:0{largura}d}de{n}.pdf"
        nucleo.substituir(p["arquivo"], final)
        p["arquivo"] = final
        p["num"], p["de"] = i, n
        p["assinado"] = assinado

    if navegacao:
        completar_partes(provisorias, marcadores, total, alvo, avisar)
    return provisorias


def completar_partes(partes, marcadores, total_paginas, alvo, avisar):
    """2ª passagem: sumário completo e links certos em cada parte."""
    # página do original -> (parte, posição dentro dela); faixas: a 1ª faixa
    mapa = {}
    for parte in partes:
        for j, g in enumerate(range(parte["pag_ini"] - 1, parte["pag_fim"])):
            mapa.setdefault(g, (parte, j))

    for parte in partes:
        def onde(g, atual=parte):
            if g not in mapa:
                return None
            destino, j = mapa[g]
            if destino is atual:
                return ("local", j)
            return ("remoto", destino["arquivo"].name, j)
        tmp = nucleo.completar_navegacao(parte["arquivo"], marcadores, onde,
                                         parte["pag_ini"] - 1, total_paginas)
        tam = tmp.stat().st_size
        if tam <= alvo or parte["excede"]:
            nucleo.substituir(tmp, parte["arquivo"])
            parte["tamanho"] = tam
        else:
            nucleo.apagar(tmp)
            avisar(f"      aviso: sem espaço para o sumário em {parte['arquivo'].name}; "
                   "os links dessa parte foram mantidos só para as páginas dela")


# --------------------------------------------------------------------------
# Agrupamento
# --------------------------------------------------------------------------
def empacotar_sequencial(pecas, capacidade: int, por_pasta: bool, tamanho=lambda p: p.custo):
    """
    Agrupamento sequencial: percorre as peças na ordem e enche um pacote de
    cada vez; quando a próxima não cabe, fecha o pacote e abre o seguinte.
    (Usado no formato ZIP, onde o tamanho final é calculável de antemão.)
    """
    lotes, atual = [], None
    for p in pecas:
        mudou_pasta = por_pasta and atual is not None and atual["itens"][-1].grupo != p.grupo
        if atual is None or tamanho(p) > atual["livre"] or mudou_pasta:
            atual = {"livre": capacidade, "itens": []}
            lotes.append(atual)
        atual["itens"].append(p)
        atual["livre"] -= tamanho(p)
    return lotes


def empacotar_compacto(pecas, capacidade: int):
    """Menor número de pacotes ZIP: maior peça primeiro, no 1º pacote em que couber."""
    ordenadas = sorted(pecas, key=lambda p: p.custo, reverse=True)
    if not ordenadas:
        return []
    menor = ordenadas[-1].custo
    lotes, abertos = [], []
    for p in ordenadas:
        escolhido = None
        for lote in abertos:
            if p.custo <= lote["livre"]:
                escolhido = lote
                break
        if escolhido is None:
            escolhido = {"livre": capacidade, "itens": []}
            lotes.append(escolhido)
            abertos.append(escolhido)
        escolhido["itens"].append(p)
        escolhido["livre"] -= p.custo
        if escolhido["livre"] < menor:
            abertos.remove(escolhido)
    for lote in lotes:
        lote["itens"].sort(key=lambda p: p.chave)
    lotes.sort(key=lambda l: l["itens"][0].chave)
    return lotes


def gravar_zip(destino: Path, itens) -> int:
    tmp = nucleo.nome_unico(destino.parent, "." + destino.stem, ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED,
                         allowZip64=True, strict_timestamps=False) as zf:
        for p in itens:
            info = zipfile.ZipInfo(p.nome_zip, date_time=data_zip(p.data_ns))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            try:
                with open(p.caminho, "rb") as f, \
                        zf.open(info, "w", force_zip64=p.tamanho >= 2**31) as d:
                    shutil.copyfileobj(f, d, 1024 * 1024)
            except OSError as e:
                raise SystemExit(f"\nERRO ao ler {p.caminho}: {e}")
    nucleo.substituir(tmp, destino)
    return destino.stat().st_size


def _dados_peca(p: Peca) -> dict:
    return dict(arquivo=p.caminho, doc=p.original, peca=p.nome_zip, parte=p.parte,
                pag_orig=p.pag_orig, faixa=p.faixa, data=p.data_ns / 1e9)


def empacotar_pdf(pecas, meta: int, pasta: Path, por_pasta: bool, progresso, avisar):
    """
    Agrupamento sequencial em PDF único. Como a união muda um pouco o
    tamanho, cada pacote é gravado e MEDIDO: se passou do teto, o último
    documento sai e vai para o próximo pacote; se sobrou espaço, tenta
    incluir o próximo. Devolve lista de pacotes (dict: nome, itens,
    tamanho, entradas).
    """
    pacotes, i, razao, n = [], 0, 1.0, 0
    total = sum(p.tamanho for p in pecas)
    feito = 0
    while i < len(pecas):
        n += 1
        nome = f"_Pacote_{n:05d}.pdf"      # nome provisório; o definitivo vem no fim
        destino = pasta / nome
        # estimativa inicial pela soma dos tamanhos (corrigida pelo último pacote)
        j, soma = i, 0
        while j < len(pecas):
            if j > i and por_pasta and pecas[j].grupo != pecas[j - 1].grupo:
                break
            if j > i and (soma + pecas[j].tamanho) * razao > meta * 0.995:
                break
            soma += pecas[j].tamanho
            j += 1
        encolheu, extras = False, 0
        while True:
            tam, entradas = nucleo.montar_pacote([_dados_peca(p) for p in pecas[i:j]],
                                                 destino, f"Pacote {n}")
            margem = nucleo.margem_navegacao(entradas)
            if tam + margem > meta and j - i > 1:
                j -= 1
                encolheu = True
                continue
            pode_crescer = (not encolheu and extras < 3 and j < len(pecas)
                            and not (por_pasta and pecas[j].grupo != pecas[j - 1].grupo)
                            and tam + margem + pecas[j].tamanho * razao <= meta * 0.995)
            if pode_crescer:
                j += 1
                extras += 1
                continue
            break
        soma_real = sum(p.tamanho for p in pecas[i:j])
        if soma_real:
            razao = max(0.5, min(1.5, tam / soma_real))
        pacotes.append(dict(nome=nome, itens=pecas[i:j], tamanho=tam, entradas=entradas,
                            caminho=destino))
        feito += soma_real
        progresso("pacotes", feito, total, f"pacote {n}")
        if n % 10 == 0:
            avisar(f"   {n} pacotes gravados ({feito / max(total, 1):.0%})")
        i = j
    return pacotes


def empacotar_zip(pecas, meta: int, capacidade: int, pasta: Path, por_pasta: bool,
                  progresso, avisar):
    """
    Agrupamento sequencial em .zip com TRANSBORDO pelo tamanho real: soma os
    PDFs na ordem enquanto cabem; grava o .zip e mede o arquivo em disco; se
    passou do teto, o último PDF sai, o .zip é regravado e esse PDF abre o
    próximo pacote. A ordem nunca é quebrada.
    """
    pacotes, i, n = [], 0, 0
    total = sum(p.tamanho for p in pecas)
    feito = 0
    while i < len(pecas):
        n += 1
        destino = pasta / f"_Pacote_{n:05d}.zip"     # nome provisório
        j, livre = i, capacidade
        while j < len(pecas):
            if j > i and por_pasta and pecas[j].grupo != pecas[j - 1].grupo:
                break
            if j > i and pecas[j].custo > livre:
                break
            livre -= pecas[j].custo
            j += 1
        while True:
            tam = gravar_zip(destino, pecas[i:j])
            if tam > meta and j - i > 1:          # transbordo: o excedente vai para o próximo
                avisar(f"   transbordo: {pecas[j - 1].nome_zip} passa para o pacote seguinte")
                j -= 1
                continue
            break
        pacotes.append(dict(itens=pecas[i:j], caminho=destino, tamanho=tam))
        feito += sum(p.tamanho for p in pecas[i:j])
        progresso("pacotes", feito, total, f"pacote {n}")
        if n % 25 == 0:
            avisar(f"   {n} pacotes gravados ({feito / max(total, 1):.0%})")
        i = j
    return pacotes


def nomear_pacotes(pacotes, ext: str):
    """Nomes definitivos: Pacote_01, Pacote_02... (3 dígitos se passar de 99, etc.)."""
    largura = max(2, len(str(len(pacotes))))
    for k, pk in enumerate(pacotes, 1):
        nome = f"Pacote_{k:0{largura}d}.{ext}"
        final = pk["caminho"].with_name(nome)
        nucleo.substituir(pk["caminho"], final)
        pk["caminho"], pk["nome"] = final, nome


def finalizar_navegacao(pacotes, meta: int, avisar):
    """2ª passagem: sumário completo e links entre pacotes (só onde há navegação)."""
    mapa = {}
    for pk in pacotes:
        for e in pk["entradas"]:
            for j in range(e["n"]):
                g = e["orig_ini"] if e.get("faixa") else e["orig_ini"] + j
                mapa.setdefault((e["doc"], g), (pk["nome"], e["pag"] + j))
    for pk in pacotes:
        if not any(e.get("nav") for e in pk["entradas"]):
            continue
        tmp = nucleo.finalizar_pacote(pk["caminho"], pk["entradas"], mapa, pk["nome"])
        tam = tmp.stat().st_size
        if tam <= meta:
            nucleo.substituir(tmp, pk["caminho"])
            pk["tamanho"] = tam
        else:
            nucleo.apagar(tmp)
            avisar(f"   aviso: sem espaço para o sumário completo em {pk['nome']}; "
                   "ficou só o marcador de cada documento")


# --------------------------------------------------------------------------
# Apoio
# --------------------------------------------------------------------------
def listar_pdfs(entrada: Path, ignorar: set):
    achados, outros = [], 0
    for raiz, pastas, arquivos in os.walk(entrada):
        raiz_p = Path(raiz)
        # não entra nas pastas que o próprio programa cria
        pastas[:] = [d for d in pastas if (raiz_p / d).resolve() not in ignorar]
        for nome in arquivos:
            if nome.lower().endswith(".pdf"):
                achados.append(raiz_p / nome)
            else:
                outros += 1
    return achados, outros


def ler_cache(arq: Path) -> dict:
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def salvar_cache(arq: Path, cache: dict):
    tmp = arq.with_name(arq.name + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    nucleo.substituir(tmp, arq)


def cache_valido(item, tamanho, mtime, alvo, assinados, saida: Path) -> bool:
    if not item or item.get("tamanho") != tamanho or item.get("mtime") != mtime \
            or item.get("alvo") != alvo or item.get("versao") != VERSAO_CORTE \
            or item.get("assinados") != assinados:
        return False
    for p in item["partes"]:
        f = saida / p["arquivo"]
        if not f.is_file() or f.stat().st_size != p["tamanho"]:
            return False
    return True


def pedir_pasta(pergunta: str) -> str:
    return input(pergunta).strip().strip('"').strip("'")


# --------------------------------------------------------------------------
# Programa principal
# --------------------------------------------------------------------------
def main():
    global UNIDADE
    for fluxo in (sys.stdout, sys.stderr):      # nomes com acento não derrubam o print
        try:
            fluxo.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    interativo = len(sys.argv) == 1
    ap = argparse.ArgumentParser(
        description="Particiona PDFs grandes (regra 1) e agrupa em pacotes (regra 2).")
    ap.add_argument("entrada", nargs="?", help="pasta com os PDFs originais")
    ap.add_argument("saida", nargs="?",
                    help="pasta de saída (padrão: a própria pasta de entrada); os pacotes "
                         f"ficam na subpasta {PASTA_PACOTES}")
    ap.add_argument("--meta-mb", type=float, default=48.0,
                    help="teto de cada pacote em MB (padrão: 48)")
    ap.add_argument("--unidade", choices=["windows", "decimal"], default="windows",
                    help="windows: 1 MB = 1.048.576 bytes (padrão); decimal: 1.000.000")
    ap.add_argument("--mb-windows", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--formato", choices=["zip", "pdf"], default="zip",
                    help="zip: os PDFs inteiros dentro de um .zip sem compressão (padrão); "
                         "pdf: PDF único com as páginas unidas")
    ap.add_argument("--ordem", choices=list(ORDENS), default="medicao",
                    help="medicao: pelo número da medição no nome, sem número vão para o fim "
                         "por data de criação (padrão); lista: ordem do arquivo --lista; "
                         "natural: pelo nome; data: data de modificação; criacao: data de "
                         "criação registrada no PDF; criacao-arquivo: data de criação do arquivo")
    ap.add_argument("--lista", help="arquivo .txt com a ordem definida (um nome por linha)")
    ap.add_argument("--por-pasta", action="store_true",
                    help="não mistura subpastas de 1º nível no mesmo pacote")
    ap.add_argument("--compactar", action="store_true",
                    help="(só ZIP) menor número de pacotes, sem seguir a ordem")
    ap.add_argument("--manter-partes", action="store_true",
                    help="guarda também os volumes particionados fora dos pacotes")
    ap.add_argument("--cortar-assinados", action="store_true",
                    help="particiona também PDFs com assinatura digital (a assinatura deixa "
                         "de valer); sem esta opção eles vão para as exceções")
    ap.add_argument("--progresso", action="store_true",
                    help="emite linhas @@PROGRESSO/@@RESUMO (usado pela interface)")
    args = ap.parse_args()
    if args.compactar and args.formato == "pdf":
        ap.error("--compactar só vale com --formato zip (no PDF único a regra é sequencial)")
    if args.ordem == "lista" and not args.lista:
        ap.error("--ordem lista precisa de --lista arquivo.txt")
    if args.mb_windows:
        args.unidade = "windows"
    UNIDADE = MIB if args.unidade == "windows" else 1_000_000

    def progresso(etapa: str, atual: int, total: int, texto: str = ""):
        if args.progresso:
            print(f"@@PROGRESSO|{etapa}|{atual}|{total}|{texto}", flush=True)

    def avisar(texto):
        print(texto, flush=True)

    if interativo:
        args.entrada = pedir_pasta("Pasta com os PDFs originais: ")
        args.saida = pedir_pasta("Pasta de saída (Enter = a mesma): ") or None
    if not args.entrada:
        ap.error("informe a pasta de entrada")

    entrada = Path(args.entrada).expanduser().resolve()
    saida = Path(args.saida).expanduser().resolve() if args.saida else entrada
    if not entrada.is_dir():
        sys.exit(f"Pasta de entrada não encontrada: {entrada}")
    saida.mkdir(parents=True, exist_ok=True)

    meta = int(round(args.meta_mb * UNIDADE))
    pasta_pacotes = saida / PASTA_PACOTES
    pasta_trab = saida / "_trabalho"
    pasta_partes = pasta_trab / "partes"
    pasta_exc = saida / "EXCECOES"
    arq_cache = pasta_trab / "partes.json"
    if args.formato == "zip":
        capacidade = meta - RESERVA_FIXA_ZIP
    else:
        capacidade = meta - RESERVA_PACOTE      # maior peça que cabe sozinha num pacote
    inicio = time.time()
    pecas, excecoes = [], []
    unid = "MB" + (" (Windows)" if args.unidade == "windows" else " (decimal)")

    # ---- 1. levantamento ---------------------------------------------------
    ignorar = {pasta_pacotes.resolve(), pasta_trab.resolve(), pasta_exc.resolve(),
               (saida / "LOTES").resolve()}
    if saida != entrada:
        ignorar.add(saida)
    pdfs, outros = listar_pdfs(entrada, ignorar)
    if not pdfs:
        sys.exit("Nenhum PDF encontrado na pasta de entrada.")
    info, estats = [], {}
    for p in pdfs:
        rel = p.relative_to(entrada).as_posix()
        try:
            st = p.stat()
        except OSError as e:
            excecoes.append((rel, "", "", f"NÃO FOI POSSÍVEL LER O ARQUIVO: {e}"))
            continue
        info.append((p, rel, st.st_size, st.st_mtime_ns))
        estats[rel] = st

    lista = {}
    if args.ordem == "lista":
        arq_lista = Path(args.lista).expanduser()
        if not arq_lista.is_file():
            sys.exit(f"Arquivo da lista de ordem não encontrado: {arq_lista}")
        lista = ler_lista_ordem(arq_lista)
        print(f"Ordem definida: {len(lista)} nome(s) em {arq_lista.name}")
    if args.ordem in ("criacao", "medicao"):
        print(f"Lendo números de medição/datas de {len(info)} PDFs...", flush=True)
    ordem_info = calcular_ordem(info, args.ordem, lista, estats, progresso)
    info.sort(key=lambda i: ordem_info[i[1]][0])
    fora = sum(1 for v in ordem_info.values() if "vai para o fim" in v[2])
    encaixados = sum(1 for v in ordem_info.values() if "logo após a última" in v[2])
    if encaixados:
        print(f"Atenção: {encaixados} arquivo(s) sem número de medição foram colocados logo após "
              "a última medição da própria pasta (veja a coluna criterio_da_ordem no índice)")
    if fora:
        print(f"Atenção: {fora} arquivo(s) sem posição definida foram para o fim da fila "
              "(veja a coluna criterio_da_ordem no índice)")

    def info_data(rel):
        return [ordem_info[rel][1], ordem_info[rel][2]]

    def precisa_cortar(rel, tam):
        return (custo_no_zip(rel, tam) if args.formato == "zip" else tam) > capacidade

    total_bytes = sum(i[2] for i in info)
    grandes = [i for i in info if precisa_cortar(i[1], i[2])]
    grandes_bytes = sum(i[2] for i in grandes)

    print(f"PDFs encontrados : {len(info)}  ({total_bytes / UNIDADE / 1000:.2f} GB)")
    print(f"Acima do teto    : {len(grandes)}  ({grandes_bytes / UNIDADE / 1000:.2f} GB) "
          "-> serão particionados (regra 1)")
    if outros:
        print(f"Ignorados        : {outros} arquivo(s) que não são PDF")
    print(f"Teto por pacote  : {args.meta_mb:g} {unid} = {meta:,} bytes".replace(",", "."))
    print(f"Formato / ordem  : {'PDF único' if args.formato == 'pdf' else 'ZIP'} / "
          f"{ORDENS[args.ordem]}")

    precisa = total_bytes + 200 * MIB + (grandes_bytes if args.manter_partes else 0)
    livre = shutil.disk_usage(saida).free
    if livre < precisa:
        sys.exit(f"\nEspaço insuficiente na pasta de saída: precisa de "
                 f"{precisa / 1e9:.1f} GB livres, há {livre / 1e9:.1f} GB.")

    # ---- 2. regra 1: partição dos grandes -----------------------------------
    pasta_trab.mkdir(parents=True, exist_ok=True)
    cache = ler_cache(arq_cache)
    nomes_usados = set()
    n_cortados = n_partes = n_faixas = n_assinados = 0
    n_grandes, i_grande = len(grandes), 0

    def nome_unico(nome: str) -> str:
        """Evita duas peças com o mesmo caminho."""
        cand, i = nome, 2
        while cand.lower() in nomes_usados:
            raiz, ext = os.path.splitext(nome)
            cand = f"{raiz}_{i}{ext}"
            i += 1
        nomes_usados.add(cand.lower())
        return cand

    for caminho, rel, tamanho, _ in info:          # reserva os nomes originais
        if not precisa_cortar(rel, tamanho):
            nomes_usados.add(rel.lower())

    for ordem_doc, (caminho, rel, tamanho, mtime) in enumerate(info, 1):
        grupo = rel.split("/")[0] if "/" in rel else ""
        chave_doc = ordem_info[rel][0]
        if not precisa_cortar(rel, tamanho):
            pecas.append(Peca(caminho, rel, rel, 0, "", "", tamanho, grupo, False, mtime,
                              chave=(chave_doc, 0)))
            continue

        rel_p = Path(rel)
        subpasta = rel_p.parent.as_posix()
        subpasta = "" if subpasta == "." else subpasta
        # alvo do volume: o que cabe sozinho num pacote
        if args.formato == "zip":
            alvo = capacidade - custo_no_zip(rel + "_parte000de000", 0)
        else:
            alvo = capacidade
        print(f"[{ordem_doc}/{len(info)}] particionando {rel}  ({tamanho / UNIDADE:.1f} MB)",
              flush=True)
        progresso("corte", i_grande, n_grandes, rel)
        i_grande += 1

        item = cache.get(rel)
        if cache_valido(item, tamanho, mtime, alvo, args.cortar_assinados, saida):
            partes = item["partes"]
            motivo_unico = item.get("motivo", "")
            print("      (já particionado em execução anterior, reaproveitado)")
        else:
            try:
                brutas = cortar_pdf(caminho, alvo, pasta_partes / subpasta, rel_p.stem,
                                    args.cortar_assinados, avisar=avisar)
            except Assinado:
                n_assinados += 1
                excecoes.append((rel, "", mb(tamanho),
                                 "PDF COM ASSINATURA DIGITAL acima do teto: não foi particionado "
                                 "porque o corte invalida a assinatura. Envie por outro meio "
                                 "ou use a opção de particionar também os assinados"))
                print("      ! tem assinatura digital: não particionado (veja excecoes.csv)")
                continue
            except Exception as e:   # senha, arquivo corrompido etc.
                excecoes.append((rel, "", mb(tamanho),
                                 f"NÃO FOI POSSÍVEL ABRIR/PARTICIONAR: {type(e).__name__}: {e}"))
                print(f"      ! não foi possível particionar: {type(e).__name__}")
                continue
            partes, motivo_unico = [], ""
            if len(brutas) == 1 and brutas[0]["excede"]:
                # documento de 1 página: o "volume" seria uma cópia do original
                motivo_unico = brutas[0]["motivo"]
                nucleo.apagar(brutas[0]["arquivo"])
                brutas = []
            for b in brutas:
                arq = b["arquivo"]
                if b["excede"]:
                    dest = pasta_exc / subpasta / arq.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    nucleo.substituir(arq, dest)
                    arq = dest
                partes.append(dict(arquivo=arq.relative_to(saida).as_posix(),
                                   num=b["num"], de=b["de"],
                                   pag_ini=b["pag_ini"], pag_fim=b["pag_fim"],
                                   tamanho=b["tamanho"], excede=b["excede"],
                                   motivo=b["motivo"], faixa=b["faixa"],
                                   assinado=b["assinado"]))
            cache[rel] = dict(tamanho=tamanho, mtime=mtime, alvo=alvo, partes=partes,
                              versao=VERSAO_CORTE, assinados=args.cortar_assinados,
                              motivo=motivo_unico)
            salvar_cache(arq_cache, cache)

        if not partes:
            excecoes.append((rel, "", mb(tamanho),
                             "DOCUMENTO DE 1 PÁGINA ACIMA DO TETO que não pôde ser "
                             f"dividida sem perda: {motivo_unico}"))
            print("      ! 1 página só, acima do teto, e não pôde ser dividida sem perda")
            continue
        n_cortados += 1
        for p in partes:
            arq = saida / p["arquivo"]
            parte = f'{p["num"]}/{p["de"]}'
            if p.get("faixa"):
                pag = f'{p["pag_ini"]} (faixa {p["faixa"]})'
            else:
                pag = f'{p["pag_ini"]}-{p["pag_fim"]}'
            if p["excede"]:
                excecoes.append((rel, arq.relative_to(saida).as_posix(), mb(p["tamanho"]),
                                 f"PÁGINA {p['pag_ini']} SOZINHA PASSA DO TETO e não pôde ser "
                                 f"dividida sem perda: {p.get('motivo', '')} (parte {parte})"))
                continue
            obs = []
            if p.get("faixa"):
                obs.append("página dividida em faixas sem perda; remontar_pdfs.py junta de volta")
                if p["faixa"].startswith("1/"):
                    n_faixas += 1
            if p.get("assinado"):
                obs.append("ASSINATURA DIGITAL INVALIDADA PELO CORTE")
            n_partes += 1
            nome_peca = nome_unico((f"{subpasta}/" if subpasta else "") + arq.name)
            pecas.append(Peca(arq, nome_peca, rel, p["num"], parte, pag,
                              p["tamanho"], grupo, True, mtime, "; ".join(obs),
                              pag_orig=p["pag_ini"] - 1, faixa=bool(p.get("faixa")),
                              chave=(chave_doc, p["num"])))

    progresso("corte", n_grandes, n_grandes)

    # ---- 3. regra 2: trava de excesso ---------------------------------------
    limite_peca = capacidade
    travados = [p for p in pecas if
                (p.custo if args.formato == "zip" else p.tamanho) > limite_peca]
    for p in travados:
        excecoes.append((p.original, p.nome_zip, mb(p.tamanho),
                         f"TRAVA DE EXCESSO: arquivo de {mb(p.tamanho)} MB passa do teto "
                         "sozinho e foi ignorado no agrupamento"))
        print(f"! trava de excesso: {p.nome_zip} ({mb(p.tamanho)} MB) ignorado")
    pecas = [p for p in pecas if p not in travados]
    pecas.sort(key=lambda p: p.chave)

    # ---- 4. regra 2: agrupamento --------------------------------------------
    pasta_pacotes.mkdir(parents=True, exist_ok=True)
    for antigo in pasta_pacotes.iterdir():          # limpa pacotes de execução anterior
        if re.fullmatch(r"_?Pacote_\d+(\.__nav__)?\.(pdf|zip)(\.tmp)?", antigo.name) \
                or antigo.name.endswith(".tmp"):
            nucleo.apagar(antigo)

    print(f"\nAgrupando {len(pecas)} arquivo(s) em pacotes de até {args.meta_mb:g} MB...",
          flush=True)
    linhas, estouros, maior = [], [], 0
    if args.formato == "pdf":
        pacotes = empacotar_pdf(pecas, meta, pasta_pacotes, args.por_pasta, progresso, avisar)
        nomear_pacotes(pacotes, "pdf")
        print("Ajustando sumário e links entre pacotes...", flush=True)
        finalizar_navegacao(pacotes, meta, avisar)
        for pk in pacotes:
            maior = max(maior, pk["tamanho"])
            if pk["tamanho"] > meta:
                estouros.append(pk["nome"])
            for p, e in zip(pk["itens"], pk["entradas"]):
                linhas.append([pk["nome"], p.grupo, p.nome_zip, p.original,
                               data_texto(p.data_ns), *info_data(p.original),
                               p.parte, p.paginas or f"1-{e['n']}",
                               f"{e['pag'] + 1}-{e['pag'] + e['n']}", p.tamanho, mb(p.tamanho),
                               mb(pk["tamanho"]), p.obs])
        n_pacotes = len(pacotes)
    else:
        if args.compactar:
            pacotes = []
            for n, lote in enumerate(empacotar_compacto(pecas, capacidade), 1):
                destino = pasta_pacotes / f"_Pacote_{n:05d}.zip"
                pacotes.append(dict(itens=lote["itens"], caminho=destino,
                                    tamanho=gravar_zip(destino, lote["itens"])))
        else:
            pacotes = empacotar_zip(pecas, meta, capacidade, pasta_pacotes, args.por_pasta,
                                    progresso, avisar)
        nomear_pacotes(pacotes, "zip")
        for pk in pacotes:
            maior = max(maior, pk["tamanho"])
            if pk["tamanho"] > meta:
                estouros.append(pk["nome"])
            for p in pk["itens"]:
                linhas.append([pk["nome"], p.grupo, p.nome_zip, p.original,
                               data_texto(p.data_ns), *info_data(p.original),
                               p.parte, p.paginas, "", p.tamanho, mb(p.tamanho),
                               mb(pk["tamanho"]), p.obs])
        n_pacotes = len(pacotes)

    if not args.manter_partes:
        for p in pecas:
            if p.temporario:
                nucleo.apagar(p.caminho)

    # ---- 5. índice e exceções -----------------------------------------------
    nucleo.gravar_csv(saida / "indice.csv", ["pacote", "pasta", "arquivo", "documento_original", "data_modificacao",
                    "valor_da_ordem", "criterio_da_ordem", "parte", "paginas_no_original", "paginas_no_pacote", "tamanho_bytes",
                    "tamanho_MB", "tamanho_do_pacote_MB", "observacao"], linhas, avisar)

    arq_exc = saida / "excecoes.csv"
    if excecoes:
        nucleo.gravar_csv(arq_exc, ["documento_original", "arquivo_separado", "tamanho_MB",
                                    "motivo"], excecoes, avisar)
    else:
        nucleo.apagar(arq_exc, tentativas=3)

    if not args.manter_partes:
        shutil.rmtree(pasta_trab, ignore_errors=True)

    # ---- 6. resumo ----------------------------------------------------------
    empacotado = sum(p.tamanho for p in pecas)
    minimo = math.ceil(empacotado / capacidade) if empacotado else 0
    ocup = (empacotado / (n_pacotes * meta) * 100) if n_pacotes else 0
    print("\n================ RESUMO ================")
    print(f"PDFs lidos           : {len(info)}")
    print(f"PDFs particionados   : {n_cortados}  (em {n_partes} volumes)")
    if n_faixas:
        print(f"Páginas em faixas    : {n_faixas}  (divididas sem perda)")
    if n_assinados:
        print(f"Assinados não cortados: {n_assinados}  -> veja excecoes.csv")
    print(f"Pacotes gerados      : {n_pacotes}  (mínimo teórico: {minimo})")
    print(f"Formato / ordem      : {'PDF único' if args.formato == 'pdf' else 'ZIP'} / "
          f"{ORDENS[args.ordem]}")
    print(f"Maior pacote         : {maior / UNIDADE:.2f} MB  (teto {args.meta_mb:g} {unid})")
    print(f"Ocupação média       : {ocup:.1f}%")
    print(f"Exceções/travas      : {len(excecoes)}" + ("  -> veja excecoes.csv" if excecoes else ""))
    print(f"Tempo                : {(time.time() - inicio) / 60:.1f} min")
    print(f"Pacotes em           : {pasta_pacotes}")
    if estouros:
        print("\nATENÇÃO: pacotes acima do teto (não deveria acontecer): " + ", ".join(estouros))
    if args.progresso:
        resumo = dict(pdfs=len(info), cortados=n_cortados, partes=n_partes,
                      lotes=n_pacotes, minimo=minimo, maior_mb=round(maior / UNIDADE, 2),
                      meta_mb=args.meta_mb, ocupacao=round(ocup, 1),
                      excecoes=len(excecoes), estouros=estouros,
                      faixas=n_faixas, assinados=n_assinados, travados=len(travados),
                      formato=args.formato, ordem=args.ordem,
                      unidade=args.unidade,
                      minutos=round((time.time() - inicio) / 60, 1), saida=str(saida),
                      pacotes=str(pasta_pacotes))
        print("@@RESUMO|" + json.dumps(resumo, ensure_ascii=False), flush=True)
    if interativo:
        input("\nEnter para fechar...")


if __name__ == "__main__":
    main()

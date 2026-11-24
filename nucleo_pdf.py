# -*- coding: utf-8 -*-
"""
nucleo_pdf.py
=============
Rotinas compartilhadas por organizar_pdfs.py e remontar_pdfs.py:

  * navegação: sumário (marcadores) e links internos, para que continuem
    funcionando nas partes e voltem exatamente ao original na remontagem;
  * faixas: divisão SEM PERDA de uma página gigante em faixas horizontais
    (uma por arquivo) e a junção de volta, com os mesmos pixels;
  * otimização sem perda e detecção de assinatura digital.

Nada aqui recomprime imagens com perda: os dados de imagem são apenas
decodificados de filtros sem perda (Flate, LZW, RunLength, ASCII) e
recodificados em Flate, que também é sem perda.
"""

import io
import json
import math
import os
import time
import uuid
from pathlib import Path, PurePosixPath

import pikepdf
from pikepdf import Array, Dictionary, Name, String

# Marcas privadas gravadas nas partes (ignoradas pelos leitores de PDF)
CHAVE_PAG = "/OrgPDFsPag"        # anotação de link: página de destino no original (0 = 1ª)
CHAVE_VIS = "/OrgPDFsVis"        # anotação de link: modo de exibição do destino
CHAVE_DOC = "/OrgPDFs"           # Root da parte: dados do documento original
CHAVE_FAIXA = "/OrgPDFsFaixa"    # página: esta página é uma faixa de uma página gigante
CHAVE_PACOTE = "/OrgPDFsPacote"  # Root do pacote: lista dos documentos e onde começam

FILTROS_SEM_PERDA = {"/FlateDecode", "/Fl", "/LZWDecode", "/LZW", "/RunLengthDecode",
                     "/RL", "/ASCIIHexDecode", "/AHx", "/ASCII85Decode", "/A85"}
NOMES_FILTRO = {"/DCTDecode": "JPEG", "/DCT": "JPEG", "/JPXDecode": "JPEG 2000",
                "/JBIG2Decode": "JBIG2", "/CCITTFaxDecode": "CCITT (fax)"}


# --------------------------------------------------------------------------
# Arquivos no Windows: antivírus, OneDrive e indexador às vezes seguram um
# arquivo recém-gravado por alguns instantes; renomear ou apagar nesse
# momento dá "Acesso negado" (WinError 5/32). Estas funções esperam e tentam
# de novo (até ~35 s) antes de desistir.
# --------------------------------------------------------------------------
_SIMULAR = int(os.environ.get("ORGPDFS_SIMULAR_TRAVA", "0") or 0)   # só para testes


def _espera(tentativa: int):
    time.sleep(min(0.1 * (tentativa + 1), 1.0))


def substituir(origem, destino, tentativas: int = 40):
    """os.replace com novas tentativas enquanto o Windows nega o acesso."""
    for i in range(tentativas):
        try:
            if i < _SIMULAR:
                raise PermissionError(13, "trava simulada para teste", str(destino))
            os.replace(origem, destino)
            return
        except PermissionError:
            if i == tentativas - 1:
                raise
            _espera(i)


def apagar(caminho, tentativas: int = 20) -> bool:
    """Apaga se puder; se o arquivo continuar travado, deixa para a limpeza final."""
    for i in range(tentativas):
        try:
            if i < _SIMULAR:
                raise PermissionError(13, "trava simulada para teste", str(caminho))
            Path(caminho).unlink(missing_ok=True)
            return True
        except PermissionError:
            _espera(i)
    return False


def nome_unico(pasta: Path, prefixo: str, sufixo: str = ".pdf") -> Path:
    """Arquivo temporário novo (nunca sobrescreve um que possa estar travado)."""
    return pasta / f"{prefixo}.{uuid.uuid4().hex[:10]}{sufixo}"


def gravar_csv(caminho: Path, cabecalho, linhas, avisar=print) -> Path:
    """
    Grava um CSV (;) para o Excel. Se o arquivo estiver aberto no Excel e o
    Windows não deixar sobrescrever, grava com outro nome e avisa.
    """
    import csv
    from datetime import datetime
    alvos = [caminho, caminho.with_name(
        f"{caminho.stem}_{datetime.now():%Y%m%d_%H%M%S}{caminho.suffix}")]
    for k, alvo in enumerate(alvos):
        for i in range(5):
            try:
                with open(alvo, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f, delimiter=";")
                    w.writerow(cabecalho)
                    w.writerows(linhas)
                if k:
                    avisar(f"AVISO: {caminho.name} está aberto em outro programa (Excel?); "
                           f"gravado como {alvo.name}")
                return alvo
            except PermissionError:
                _espera(i)
    raise PermissionError(13, "não foi possível gravar", str(caminho))


class NaoDivisivel(Exception):
    """A página não pode ser dividida sem perda; a mensagem diz o motivo."""


# --------------------------------------------------------------------------
# Destinos (para onde aponta um link ou marcador)
# --------------------------------------------------------------------------
def indice_paginas(pdf) -> dict:
    """objgen da página -> posição (0 = 1ª página)."""
    return {p.obj.objgen: i for i, p in enumerate(pdf.pages)}


def _buscar_nome(pdf, nome):
    """Destino nomeado -> valor do destino (Array ou dicionário com /D)."""
    try:
        if isinstance(nome, Name) and "/Dests" in pdf.Root:
            if str(nome) in pdf.Root.Dests:
                return pdf.Root.Dests[str(nome)]
        chave = str(nome)[1:] if isinstance(nome, Name) else str(nome)
        if "/Names" in pdf.Root and "/Dests" in pdf.Root.Names:
            arvore = pikepdf.NameTree(pdf.Root.Names.Dests)
            if chave in arvore:
                return arvore[chave]
    except Exception:
        pass
    return None


def _para_json(v):
    if isinstance(v, Name):
        return str(v)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _de_json(v):
    if isinstance(v, str):
        return Name(v)
    return v      # número ou None (null)


def resolver_destino(pdf, idx: dict, destino):
    """Devolve (página 0-based ou None, lista de exibição em JSON)."""
    d = destino
    for _ in range(8):
        if isinstance(d, (String, Name)):
            d = _buscar_nome(pdf, d)
        elif isinstance(d, Dictionary) and "/D" in d:
            d = d.D
        else:
            break
    if not isinstance(d, Array) or len(d) == 0:
        return None, []
    alvo = d[0]
    pag = None
    if isinstance(alvo, Dictionary):
        pag = idx.get(alvo.objgen)
    else:
        try:
            pag = int(alvo)
        except (TypeError, ValueError):
            pag = None
    return pag, [_para_json(x) for x in list(d)[1:]]


def destino_do_link(anot):
    """Destino interno de uma anotação de link (ou None)."""
    try:
        if anot.get("/Subtype") != "/Link":
            return None
        if "/Dest" in anot:
            return anot.Dest
        acao = anot.get("/A")
        if isinstance(acao, Dictionary) and acao.get("/S") == "/GoTo" and "/D" in acao:
            return acao.D
    except Exception:
        pass
    return None


def anotacoes(pagina_obj):
    a = pagina_obj.get("/Annots")
    return list(a) if isinstance(a, Array) else []


def tem_links_internos(pdf) -> bool:
    for p in pdf.pages:
        for a in anotacoes(p.obj):
            if isinstance(a, Dictionary) and destino_do_link(a) is not None:
                return True
    return False


def marcar_links(src, idx, dst, ini: int, k: int, desloc: int = 0):
    """
    Nas páginas copiadas para `dst` (páginas ini..ini+k-1 de src, que em dst
    começam na posição `desloc`), grava em cada link o destino no documento
    original e já aponta corretamente os links cujo destino está dentro
    deste mesmo trecho.
    """
    for j in range(k):
        orig = anotacoes(src.pages[ini + j].obj)
        copia = anotacoes(dst.pages[desloc + j].obj)
        for a_src, a_dst in zip(orig, copia):
            if not isinstance(a_src, Dictionary) or not isinstance(a_dst, Dictionary):
                continue
            destino = destino_do_link(a_src)
            if destino is None:
                continue
            pag, vis = resolver_destino(src, idx, destino)
            if pag is None:
                continue
            a_dst[CHAVE_PAG] = pag
            a_dst[CHAVE_VIS] = Array([_de_json(v) for v in vis])
            if ini <= pag < ini + k:
                apontar_local(dst, a_dst, pag - ini + desloc)


def _vis(anot_ou_lista):
    vis = list(anot_ou_lista) if anot_ou_lista is not None else []
    return vis if vis else [Name.Fit]


def apontar_local(pdf, anot, pagina_local: int):
    anot.Dest = Array([pdf.pages[pagina_local].obj] + _vis(anot.get(CHAVE_VIS)))
    if "/A" in anot:
        del anot["/A"]


def apontar_remoto(anot, arquivo: str, pagina_local: int):
    anot.A = Dictionary(S=Name.GoToR, F=String(arquivo),
                        D=Array([pagina_local] + _vis(anot.get(CHAVE_VIS))))
    if "/Dest" in anot:
        del anot["/Dest"]


# --------------------------------------------------------------------------
# Sumário (marcadores)
# --------------------------------------------------------------------------
def ler_marcadores(pdf, idx: dict) -> list:
    """Árvore de marcadores em formato simples (JSON), com destino resolvido."""
    if "/Outlines" not in pdf.Root:
        return []

    def conv(item):
        destino = item.destination
        acao = item.action
        if destino is None and isinstance(acao, Dictionary) and acao.get("/S") == "/GoTo":
            destino = acao.get("/D")
        pag, vis = resolver_destino(pdf, idx, destino) if destino is not None else (None, [])
        return {"t": str(item.title or ""), "p": pag, "v": vis,
                "f": bool(item.is_closed), "c": [conv(c) for c in item.children]}
    try:
        with pdf.open_outline() as o:
            return [conv(i) for i in o.root]
    except Exception:
        return []


def itens_marcadores(pdf, arvore: list, onde):
    """
    Cria os itens de sumário de `arvore`. `onde(pag_global)` devolve
    ("local", página) | ("remoto", arquivo, página) | None.
    """
    def criar(n):
        alvo = onde(n["p"]) if n.get("p") is not None else None
        vis = [_de_json(v) for v in n.get("v", [])] or [Name.Fit]
        if alvo is None:
            item = pikepdf.OutlineItem(n["t"])
        elif alvo[0] == "local":
            item = pikepdf.OutlineItem(n["t"], Array([pdf.pages[alvo[1]].obj] + vis))
        else:
            item = pikepdf.OutlineItem(n["t"], action=Dictionary(
                S=Name.GoToR, F=String(alvo[1]), D=Array([alvo[2]] + vis)))
        item.is_closed = bool(n.get("f"))
        item.children.extend(criar(c) for c in n.get("c", []))
        return item
    return [criar(n) for n in arvore]


def escrever_marcadores(pdf, arvore: list, onde):
    """Grava a árvore de marcadores em `pdf` (substitui o sumário)."""
    itens = itens_marcadores(pdf, arvore, onde)
    with pdf.open_outline() as o:
        o.root.clear()
        o.root.extend(itens)


def completar_navegacao(caminho: Path, arvore: list, onde, pag_ini: int, pag_total: int):
    """
    2ª passagem numa parte já gravada: sumário completo, links apontando
    para a página certa (nesta parte ou no arquivo da outra parte) e os dados
    para a remontagem. Grava em arquivo temporário; devolve o caminho dele.
    """
    tmp = nome_unico(caminho.parent, caminho.stem + ".__nav__")
    with pikepdf.open(caminho) as pdf:
        for pagina in pdf.pages:
            for a in anotacoes(pagina.obj):
                if not isinstance(a, Dictionary) or CHAVE_PAG not in a:
                    continue
                alvo = onde(int(a[CHAVE_PAG]))
                if alvo is None:
                    continue
                if alvo[0] == "local":
                    apontar_local(pdf, a, alvo[1])
                else:
                    apontar_remoto(a, alvo[1], alvo[2])
        if arvore:
            escrever_marcadores(pdf, arvore, onde)
        pdf.Root[CHAVE_DOC] = Dictionary(
            Versao=1, PagIni=pag_ini, PagTotal=pag_total,
            Marcadores=String(json.dumps(arvore, ensure_ascii=False)))
        pdf.save(tmp, min_version=pdf.pdf_version)
    return tmp


def restaurar_navegacao(pdf, dados_doc):
    """Na remontagem: recria o sumário e aponta os links para as páginas certas."""
    total = len(pdf.pages)

    def onde(g):
        return ("local", g) if g is not None and 0 <= g < total else None

    corrigidos = 0
    for pagina in pdf.pages:
        for a in anotacoes(pagina.obj):
            if not isinstance(a, Dictionary) or CHAVE_PAG not in a:
                continue
            alvo = onde(int(a[CHAVE_PAG]))
            if alvo is not None:
                apontar_local(pdf, a, alvo[1])
                corrigidos += 1
            for chave in (CHAVE_PAG, CHAVE_VIS):
                if chave in a:
                    del a[chave]
    arvore = []
    if dados_doc is not None and "/Marcadores" in dados_doc:
        try:
            arvore = json.loads(str(dados_doc.Marcadores))
        except ValueError:
            arvore = []
    if arvore:
        escrever_marcadores(pdf, arvore, onde)
    return corrigidos, len(arvore)


# --------------------------------------------------------------------------
# Assinatura digital
# --------------------------------------------------------------------------
def esta_assinado(pdf) -> bool:
    try:
        if "/Perms" in pdf.Root:
            return True
        form = pdf.Root.get("/AcroForm")
        if not isinstance(form, Dictionary):
            return False
        pilha, vistos = list(form.get("/Fields", [])), set()
        while pilha:
            f = pilha.pop()
            if not isinstance(f, Dictionary):
                continue
            og = f.objgen
            if og != (0, 0):
                if og in vistos:
                    continue
                vistos.add(og)
            if f.get("/FT") == "/Sig" and f.get("/V") is not None:
                return True
            pilha.extend(f.get("/Kids", []))
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# Página gigante: otimização sem perda e faixas
# --------------------------------------------------------------------------
def copiar_info(src, dst):
    try:
        if "/Info" in src.trailer:
            dst.trailer.Info = dst.copy_foreign(src.trailer.Info)
    except Exception:
        pass


def gravar_otimizado(pdf, destino: Path, versao: str):
    """
    Gravação SEM PERDA mais compacta: recomprime os fluxos Flate no nível
    máximo, decodifica filtros antigos (LZW, ASCII) para Flate e agrupa os
    objetos. JPEG e afins ficam intactos.
    """
    pikepdf.settings.set_flate_compression_level(9)
    try:
        pdf.save(destino, min_version=versao, compress_streams=True,
                 recompress_flate=True,
                 stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate)
    finally:
        pikepdf.settings.set_flate_compression_level(-1)


def _filtros(stream):
    f = stream.get("/Filter")
    if f is None:
        return []
    return [str(x) for x in f] if isinstance(f, Array) else [str(f)]


def _decodificar(img, nome: str):
    """Dados brutos (pixels) de uma imagem, se o filtro for sem perda."""
    for f in _filtros(img):
        if f not in FILTROS_SEM_PERDA:
            raise NaoDivisivel(
                f"a imagem principal da página ({nome}) está em {NOMES_FILTRO.get(f, f[1:])}, "
                "formato que não pode ser dividido sem perda de qualidade")
    dados = img.read_bytes()
    altura = int(img.Height)
    if altura <= 1 or len(dados) % altura:
        raise NaoDivisivel(f"formato de imagem não reconhecido ({nome})")
    return dados, len(dados) // altura, altura


def escolher_imagem(pagina):
    """Maior imagem desenhada diretamente pela página: (nome, stream)."""
    res = pagina.obj.get("/Resources")
    xo = res.get("/XObject") if isinstance(res, Dictionary) else None
    if not isinstance(xo, Dictionary):
        raise NaoDivisivel("a página não tem imagem que possa ser dividida")
    melhor = None
    for nome, obj in xo.items():
        if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Image":
            tam = len(obj.read_raw_bytes())
            if melhor is None or tam > melhor[2]:
                melhor = (nome, obj, tam)
    if melhor is None:
        raise NaoDivisivel("a página não tem imagem que possa ser dividida")
    return melhor


def _copiar_chaves(origem, destino_stream, altura):
    for chave, valor in origem.items():
        if chave not in ("/Length", "/Filter", "/DecodeParms", "/Height", "/SMask", "/DL"):
            destino_stream[chave] = valor
    destino_stream.Height = altura


def dividir_em_faixas(src, idx, g: int, alvo: int, pasta: Path, base: str,
                      versao: str, tamanho_pagina: int):
    """
    Divide a página `g` de src em faixas horizontais, cada uma num PDF de
    até `alvo` bytes. A imagem principal é cortada por linhas de pixels
    (sem perda); o resto da página (textos, desenhos) se repete em cada
    faixa. Devolve a lista de (arquivo, tamanho). Levanta NaoDivisivel.
    """
    nome, img_src, _ = escolher_imagem(src.pages[g])
    if isinstance(img_src.get("/Mask"), pikepdf.Stream):
        raise NaoDivisivel("a imagem principal usa máscara explícita, que não é suportada")
    dados, passo, altura = _decodificar(img_src, nome)
    mascara = img_src.get("/SMask")
    if isinstance(mascara, pikepdf.Stream):
        dados_m, passo_m, altura_m = _decodificar(mascara, nome + " (transparência)")
        if altura_m != altura:
            raise NaoDivisivel("a transparência da imagem tem altura diferente da imagem")
    else:
        mascara = None
    grupo = f"{base}#p{g + 1}"

    n = max(2, math.ceil(tamanho_pagina / (alvo * 0.85)))
    for _tentativa in range(8):
        if n > altura:
            break
        cortes = [round(i * altura / n) for i in range(n + 1)]
        arquivos = []
        for i in range(n):
            r0, r1 = cortes[i], cortes[i + 1]
            destino = nome_unico(pasta, f"{base}.__faixa{i + 1}__")
            dst = pikepdf.Pdf.new()
            try:
                dst.pages.append(src.pages[g])
                copiar_info(src, dst)
                marcar_links(src, idx, dst, g, 1)
                pagina = dst.pages[0]
                xo = pagina.obj.Resources.XObject
                img = xo[nome]
                faixa = pikepdf.Stream(dst, dados[r0 * passo:r1 * passo])
                _copiar_chaves(img, faixa, r1 - r0)
                if mascara is not None:
                    m = pikepdf.Stream(dst, dados_m[r0 * passo_m:r1 * passo_m])
                    _copiar_chaves(img.SMask, m, r1 - r0)
                    faixa.SMask = m
                h = (r1 - r0) / altura
                y = 1 - r1 / altura
                forma = pikepdf.Stream(dst, f"q 1 0 0 {h:.12f} 0 {y:.12f} cm /Faixa Do Q".encode())
                forma.Type = Name.XObject
                forma.Subtype = Name.Form
                forma.BBox = Array([0, 0, 1, 1])
                forma.Resources = Dictionary(XObject=Dictionary(Faixa=faixa))
                xo[nome] = forma
                pagina.obj[CHAVE_FAIXA] = Dictionary(
                    Grupo=String(grupo), Indice=i + 1, Total=n, Nome=Name(nome),
                    Altura=altura)
                dst.remove_unreferenced_resources()
                gravar_otimizado(dst, destino, versao)
            finally:
                dst.close()
            arquivos.append((destino, destino.stat().st_size))
        maior = max(t for _, t in arquivos)
        if maior <= alvo:
            return arquivos
        for f, _ in arquivos:
            apagar(f)
        n = math.ceil(n * maior / (alvo * 0.95)) + 1
    raise NaoDivisivel("mesmo dividida em faixas finas a página continua acima do limite "
                       "(o conteúdo fora da imagem principal já é grande demais)")


def juntar_faixas(pdf):
    """
    Na remontagem: encontra as faixas de cada página gigante (já na ordem),
    reconstrói a imagem original linha por linha e deixa uma página só.
    Devolve (páginas reconstruídas, lista de problemas).
    """
    problemas, refeitas = [], 0
    i = 0
    while i < len(pdf.pages):
        marca = pdf.pages[i].obj.get(CHAVE_FAIXA)
        if not isinstance(marca, Dictionary):
            i += 1
            continue
        grupo, total = str(marca.Grupo), int(marca.Total)
        bloco = []
        j = i
        while j < len(pdf.pages) and len(bloco) < total:
            m = pdf.pages[j].obj.get(CHAVE_FAIXA)
            if not isinstance(m, Dictionary) or str(m.Grupo) != grupo:
                break
            bloco.append(pdf.pages[j])
            j += 1
        indices = [int(p.obj[CHAVE_FAIXA].Indice) for p in bloco]
        if indices != list(range(1, total + 1)):
            problemas.append(f"faixas incompletas da página {grupo.split('#p')[-1]} "
                             f"(encontradas {indices}, esperadas 1 a {total})")
            i = j
            continue
        nome = str(marca.Nome)
        partes, partes_m = [], []
        for p in bloco:
            faixa = p.obj.Resources.XObject[nome].Resources.XObject.Faixa
            partes.append(faixa.read_bytes())
            if "/SMask" in faixa:
                partes_m.append(faixa.SMask.read_bytes())
        primeira = bloco[0].obj.Resources.XObject[nome].Resources.XObject.Faixa
        imagem = pikepdf.Stream(pdf, b"".join(partes))
        _copiar_chaves(primeira, imagem, int(marca.Altura))
        if partes_m and len(partes_m) == len(partes):
            m = pikepdf.Stream(pdf, b"".join(partes_m))
            _copiar_chaves(primeira.SMask, m, int(marca.Altura))
            imagem.SMask = m
        pagina = bloco[0]
        pagina.obj.Resources.XObject[nome] = imagem
        del pagina.obj[CHAVE_FAIXA]
        for _ in range(len(bloco) - 1):
            del pdf.pages[i + 1]
        refeitas += 1
        i += 1
    return refeitas, problemas


def remover_marcas_soltas(pdf):
    """Remove marcas privadas que tenham sobrado (ex.: faixa incompleta)."""
    if CHAVE_DOC in pdf.Root:
        del pdf.Root[CHAVE_DOC]


# --------------------------------------------------------------------------
# Pacotes em PDF único (regra 2: união direta das páginas)
# --------------------------------------------------------------------------
def _info_texto(pdf) -> dict:
    out = {}
    try:
        for k, v in pdf.docinfo.items():
            if isinstance(v, (String, Name)):
                out[str(k)] = str(v)
    except Exception:
        pass
    return out


def _tem_marcas(pdf, ini, n) -> bool:
    for j in range(ini, ini + n):
        for a in anotacoes(pdf.pages[j].obj):
            if isinstance(a, Dictionary) and CHAVE_PAG in a:
                return True
    return False


def montar_pacote(pecas, destino: Path, titulo: str):
    """
    Une as páginas das peças num PDF só, sem recomprimir nada, com um
    marcador por documento e os dados para separar de volta. `pecas` é uma
    lista de dicts: arquivo, doc, peca, parte, pag_orig, faixa, data.
    Devolve (tamanho, entradas).
    """
    pdf = pikepdf.Pdf.new()
    abertos, entradas, versao = [], [], "1.4"
    try:
        for pc in pecas:
            # lido para a memória: evita manter centenas de arquivos abertos
            src = pikepdf.open(io.BytesIO(Path(pc["arquivo"]).read_bytes()))
            abertos.append(src)
            versao = max(versao, src.pdf_version)
            ini, n = len(pdf.pages), len(src.pages)
            pdf.pages.extend(src.pages)
            e = dict(doc=pc["doc"], peca=pc["peca"], pag=ini, n=n, parte=pc["parte"],
                     orig_ini=pc["pag_orig"], faixa=pc["faixa"], data=pc["data"],
                     info=_info_texto(src))
            dados = src.Root.get(CHAVE_DOC)
            if pc["parte"]:
                if isinstance(dados, Dictionary):           # parte de PDF com navegação
                    e["orig_total"] = int(dados.PagTotal)
                    try:
                        e["marc"] = json.loads(str(dados.Marcadores))
                    except ValueError:
                        e["marc"] = []
            else:
                idx = indice_paginas(src)
                marcar_links(src, idx, pdf, 0, n, desloc=ini)
                marc = ler_marcadores(src, idx)
                if marc:
                    e["marc"] = marc
                e["orig_total"] = n
            e["nav"] = bool(e.get("marc")) or _tem_marcas(pdf, ini, n)
            entradas.append(e)
        with pdf.open_outline() as o:
            o.root.extend(pikepdf.OutlineItem(PurePosixPath(e["peca"]).name, e["pag"])
                          for e in entradas)
        pdf.docinfo["/Title"] = titulo
        pdf.Root[CHAVE_PACOTE] = String(json.dumps(entradas, ensure_ascii=False))
        tmp = nome_unico(destino.parent, "." + destino.stem, ".tmp")
        pdf.save(tmp, min_version=versao)
        substituir(tmp, destino)
    finally:
        pdf.close()
        for s in abertos:
            s.close()
    return destino.stat().st_size, entradas


def margem_navegacao(entradas) -> int:
    """Espaço a reservar para o sumário completo e os links (2ª passagem)."""
    return sum(3 * len(json.dumps(e.get("marc", [])).encode()) + 16 * 1024
               for e in entradas if e.get("nav"))


def finalizar_pacote(caminho: Path, entradas, mapa: dict, nome_pacote: str):
    """
    2ª passagem num pacote: sumário de cada documento sob o marcador dele e
    links apontando para a página certa (neste pacote ou no arquivo de outro
    pacote). `mapa[(doc, página_original)] = (arquivo_do_pacote, página)`.
    Grava em arquivo temporário e devolve o caminho dele.
    """
    tmp = nome_unico(caminho.parent, caminho.stem + ".__nav__")
    with pikepdf.open(caminho) as pdf:
        def onde_doc(doc):
            def onde(g):
                alvo = mapa.get((doc, g))
                if alvo is None:
                    return None
                if alvo[0] == nome_pacote:
                    return ("local", alvo[1])
                return ("remoto", alvo[0], alvo[1])
            return onde
        itens = []
        for e in entradas:
            onde = onde_doc(e["doc"])
            for j in range(e["pag"], e["pag"] + e["n"]):
                for a in anotacoes(pdf.pages[j].obj):
                    if not isinstance(a, Dictionary) or CHAVE_PAG not in a:
                        continue
                    alvo = onde(int(a[CHAVE_PAG]))
                    if alvo is None:
                        continue
                    if alvo[0] == "local":
                        apontar_local(pdf, a, alvo[1])
                    else:
                        apontar_remoto(a, alvo[1], alvo[2])
            item = pikepdf.OutlineItem(PurePosixPath(e["peca"]).name,
                                       Array([pdf.pages[e["pag"]].obj, Name.Fit]))
            if e.get("marc"):
                item.children.extend(itens_marcadores(pdf, e["marc"], onde))
                item.is_closed = True
            itens.append(item)
        with pdf.open_outline() as o:
            o.root.clear()
            o.root.extend(itens)
        pdf.save(tmp, min_version=pdf.pdf_version)
    return tmp


def ler_pacote(pdf):
    """Lista de documentos de um pacote (ou None se não for um pacote)."""
    dados = pdf.Root.get(CHAVE_PACOTE)
    if dados is None:
        return None
    try:
        return json.loads(str(dados))
    except ValueError:
        return None


def extrair_do_pacote(pdf, e: dict, destino: Path):
    """
    Recria, a partir de um pacote, o arquivo de uma peça: a parte cortada
    (com os dados para a remontagem) ou o PDF inteiro (já com título,
    sumário e links restaurados).
    """
    out = pikepdf.Pdf.new()
    try:
        out.pages.extend(pdf.pages[e["pag"]:e["pag"] + e["n"]])
        for k, v in e.get("info", {}).items():
            out.docinfo[k] = v
        if e.get("parte"):
            if "orig_total" in e:
                out.Root[CHAVE_DOC] = Dictionary(
                    Versao=1, PagIni=int(e["orig_ini"]), PagTotal=int(e["orig_total"]),
                    Marcadores=String(json.dumps(e.get("marc", []), ensure_ascii=False)))
        else:
            restaurar_navegacao(out, Dictionary(
                Marcadores=String(json.dumps(e.get("marc", []), ensure_ascii=False))))
        destino.parent.mkdir(parents=True, exist_ok=True)
        out.save(destino, min_version=pdf.pdf_version)
    finally:
        out.close()

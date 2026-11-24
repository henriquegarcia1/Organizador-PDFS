#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
organizar_pdfs_gui.py
=====================
Interface gráfica (Qt 6 / PySide6) com três abas:
  1. Separar em lotes  -> organizar_pdfs.py
  2. Remontar PDFs     -> remontar_pdfs.py
  3. Testar            -> autoteste.py (testa o programa neste computador)

Na primeira execução instala sozinho o que faltar (PySide6 e pikepdf).
Deixe este arquivo na MESMA pasta dos outros dois e rode:

    py organizar_pdfs_gui.py        (ou dê dois cliques em Iniciar.bat)
"""

import importlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

PASTA_PROGRAMA = Path(__file__).resolve().parent
REQUISITOS = [("PySide6", "PySide6-Essentials>=6.5"), ("pikepdf", "pikepdf>=8")]


# --------------------------------------------------------------------------
# 1. Dependências (roda antes de qualquer import do Qt)
# --------------------------------------------------------------------------
def python_console() -> str:
    """Executável do Python com console (pythonw.exe não mostra saída)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        alt = exe.with_name("python.exe")
        if alt.exists():
            return str(alt)
    return str(exe)


def encerrar_com_erro(msg: str):
    print("\n" + msg)
    try:
        input("\nEnter para fechar...")
    except (EOFError, RuntimeError):
        pass
    sys.exit(1)


def faltando():
    importlib.invalidate_caches()
    return [pacote for modulo, pacote in REQUISITOS
            if importlib.util.find_spec(modulo) is None]


def garantir_dependencias():
    if sys.version_info < (3, 9):
        encerrar_com_erro("É preciso Python 3.9 ou mais novo. "
                          "Baixe em https://www.python.org/downloads/")
    pendentes = faltando()
    if not pendentes:
        return

    print("=" * 64)
    print(" Instalando dependências: " + ", ".join(pendentes))
    print(" Só acontece na primeira vez. Pode levar alguns minutos.")
    print("=" * 64, flush=True)

    py = python_console()
    if importlib.util.find_spec("pip") is None:
        subprocess.call([py, "-m", "ensurepip", "--upgrade"])

    base = [py, "-m", "pip", "install", "--disable-pip-version-check"]
    if subprocess.call(base + pendentes) != 0:
        print("\nTentando instalar só para o usuário atual...", flush=True)
        if subprocess.call(base + ["--user"] + pendentes) != 0:
            encerrar_com_erro(
                "Não foi possível instalar as dependências.\n"
                "Verifique a conexão com a internet (ou o proxy da rede) e tente\n"
                "manualmente no Prompt de Comando:\n\n"
                f'    "{py}" -m pip install ' + " ".join(pendentes))

    # uma instalação com --user pode criar a pasta agora: inclui no caminho
    import site
    try:
        usuario = site.getusersitepackages()
        if os.path.isdir(usuario) and usuario not in sys.path:
            site.addsitedir(usuario)
    except Exception:
        pass

    if faltando():
        encerrar_com_erro("A instalação terminou, mas o Python ainda não enxerga os "
                          "pacotes. Feche esta janela e abra o programa de novo.")
    print("\nDependências instaladas. Abrindo o programa...\n", flush=True)


garantir_dependencias()

# --------------------------------------------------------------------------
# 2. Interface
# --------------------------------------------------------------------------
import json  # noqa: E402
import math  # noqa: E402
import time  # noqa: E402

from PySide6.QtCore import (QProcess, QProcessEnvironment, QSettings,  # noqa: E402
                            QThread, QUrl, Signal)
from PySide6.QtGui import QDesktopServices, QFontDatabase, QTextCursor  # noqa: E402
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,  # noqa: E402
                               QFileDialog, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSizePolicy, QTabWidget,
                               QVBoxLayout, QWidget)

MIB = 1024 * 1024
TITULO = "Organizador de PDFs em lotes"
ESCALA_BARRA = 1000


def fmt_gb(n: int) -> str:
    return f"{n / MIB / 1024:.2f} GB".replace(".", ",")


def fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def mesma_pasta(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


class Levantamento(QThread):
    """Conta arquivos de uma extensão na pasta, sem travar a janela."""
    pronto = Signal(int, list)      # geração, lista de tamanhos

    def __init__(self, geracao: int, pasta: str, ignorar: str, extensao, pular=("_trabalho",)):
        super().__init__()
        self.geracao, self.pasta, self.ignorar, self.extensao = geracao, pasta, ignorar, extensao
        self.pular = {p.lower() for p in pular}

    def run(self):
        tamanhos = []
        try:
            ignorar = Path(self.ignorar).resolve() if self.ignorar else None
        except OSError:
            ignorar = None
        for raiz, pastas, arquivos in os.walk(self.pasta):
            if self.isInterruptionRequested():
                return
            pastas[:] = [d for d in pastas if d.lower() not in self.pular
                         and (ignorar is None or Path(raiz, d).resolve() != ignorar)]
            for nome in arquivos:
                if nome.lower().endswith(self.extensao):
                    try:
                        tamanhos.append(os.path.getsize(os.path.join(raiz, nome)))
                    except OSError:
                        pass
        self.pronto.emit(self.geracao, tamanhos)


class CampoPasta(QWidget):
    """Caixa de texto + botão Escolher, que também aceita arrastar a pasta."""
    alterado = Signal()

    def __init__(self, titulo_dialogo: str):
        super().__init__()
        self.titulo_dialogo = titulo_dialogo
        self.texto = QLineEdit()
        self.texto.setPlaceholderText("Escolha ou arraste uma pasta aqui")
        self.texto.setAcceptDrops(False)
        self.botao = QPushButton("Escolher…")
        self.botao.clicked.connect(self.escolher)
        self.texto.editingFinished.connect(self.alterado)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.texto, 1)
        lay.addWidget(self.botao)
        self.setAcceptDrops(True)

    def valor(self) -> str:
        return self.texto.text().strip().strip('"')

    def definir(self, caminho: str):
        self.texto.setText(os.path.normpath(caminho) if caminho else "")
        self.alterado.emit()

    def escolher(self):
        atual = self.valor() if os.path.isdir(self.valor()) else str(Path.home())
        pasta = QFileDialog.getExistingDirectory(self, self.titulo_dialogo, atual)
        if pasta:
            self.definir(pasta)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for url in ev.mimeData().urls():
            caminho = url.toLocalFile()
            if os.path.isdir(caminho):
                self.definir(caminho)
                return
            if os.path.isfile(caminho):
                self.definir(os.path.dirname(caminho))
                return


class PainelExecucao(QWidget):
    """
    Parte comum das duas abas: botões Iniciar/Cancelar, barra de progresso,
    registro e a execução do script em segundo plano (QProcess).
    Cada aba define: script, montar_argumentos(), texto_progresso(), concluido().
    """
    script = None
    prefixo = ""

    def __init__(self, cfg: QSettings):
        super().__init__()
        self.cfg = cfg
        self.processo = None
        self.cancelado = False
        self.buffer = ""
        self.resumo = None
        self.inicio = 0.0
        self.texto_atual = ""
        self.campos = []           # widgets travados durante a execução
        self.botoes_saida = []     # (botão, arquivo relativo à saída ou "")
        self.threads = set()
        self.geracao = 0

        self.raiz = QVBoxLayout(self)
        self.raiz.setSpacing(10)
        self.corpo = QVBoxLayout()
        self.raiz.addLayout(self.corpo)

        botoes = QHBoxLayout()
        self.b_iniciar = QPushButton("Iniciar")
        self.b_iniciar.setMinimumWidth(110)
        self.b_cancelar = QPushButton("Cancelar")
        self.b_cancelar.setEnabled(False)
        botoes.addWidget(self.b_iniciar)
        botoes.addWidget(self.b_cancelar)
        botoes.addStretch(1)
        self.linha_botoes = botoes
        self.raiz.addLayout(botoes)

        self.etiqueta = QLabel("Pronto.")
        self.barra = QProgressBar()
        self.barra.setRange(0, 1)
        self.barra.setValue(0)
        self.raiz.addWidget(self.etiqueta)
        self.raiz.addWidget(self.barra)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.log.setMaximumBlockCount(20000)
        self.log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.log.setPlaceholderText("O registro da execução aparece aqui.")
        self.raiz.addWidget(self.log, 1)

        self.b_iniciar.clicked.connect(self.iniciar)
        self.b_cancelar.clicked.connect(self.cancelar)

    # ----------------------------------------------------------- utilidades
    def botao_saida(self, texto: str, arquivo: str):
        b = QPushButton(texto)
        b.clicked.connect(lambda: self.abrir(os.path.join(self.pasta_saida(), arquivo)
                                             if arquivo else self.pasta_saida()))
        self.linha_botoes.addWidget(b)
        self.botoes_saida.append((b, arquivo))

    def pasta_saida(self) -> str:
        return self.saida.valor()

    def atualizar_botoes_saida(self):
        s = self.pasta_saida()
        ok = bool(s) and os.path.isdir(s) and self.processo is None
        for b, arquivo in self.botoes_saida:
            b.setEnabled(ok and (not arquivo or os.path.exists(os.path.join(s, arquivo))))

    def abrir(self, caminho: str):
        if caminho and os.path.exists(caminho):
            QDesktopServices.openUrl(QUrl.fromLocalFile(caminho))

    def contar(self, pasta: str, ignorar: str, extensao, rotulo: QLabel, pular=("_trabalho",)):
        """Dispara a contagem em segundo plano; resultado vai para info_pronta()."""
        self.geracao += 1
        for t in self.threads:
            t.requestInterruption()
        if not pasta:
            rotulo.setText(" ")
            return
        if not os.path.isdir(pasta):
            rotulo.setText("Pasta não encontrada.")
            return
        rotulo.setText("Contando os arquivos…")
        t = Levantamento(self.geracao, pasta, ignorar, extensao, pular)
        t.pronto.connect(lambda g, tam: g == self.geracao and self.info_pronta(tam))
        t.finished.connect(lambda t=t: self.threads.discard(t))
        self.threads.add(t)
        t.start()

    def info_pronta(self, tamanhos):
        pass

    # -------------------------------------------------------------- execução
    def validar_pastas(self, entrada: str, saida: str, nome_entrada: str) -> bool:
        if not entrada or not os.path.isdir(entrada):
            QMessageBox.warning(self, TITULO, f"Escolha a pasta {nome_entrada}.")
            return False
        if not saida:
            QMessageBox.warning(self, TITULO, "Escolha a pasta de saída.")
            return False
        if mesma_pasta(entrada, saida):
            QMessageBox.warning(self, TITULO,
                                "A pasta de saída precisa ser diferente da pasta de entrada.")
            return False
        if not self.script.exists():
            QMessageBox.critical(self, TITULO,
                                 f"Não encontrei o arquivo {self.script.name} em:\n"
                                 f"{PASTA_PROGRAMA}\n\nOs arquivos do programa precisam "
                                 "ficar todos na mesma pasta.")
            return False
        return True

    def iniciar(self):
        args = self.montar_argumentos()
        if args is None:
            return
        self.log.clear()
        self.inicio = time.monotonic()
        self.resumo, self.buffer, self.cancelado, self.texto_atual = None, "", False, ""
        self.etiqueta.setText("Analisando os arquivos…")
        self.barra.setRange(0, 0)

        p = QProcess(self)
        amb = QProcessEnvironment.systemEnvironment()
        amb.insert("PYTHONIOENCODING", "utf-8")
        amb.insert("PYTHONUNBUFFERED", "1")
        p.setProcessEnvironment(amb)
        p.setProcessChannelMode(QProcess.MergedChannels)
        p.setWorkingDirectory(str(PASTA_PROGRAMA))
        p.readyReadStandardOutput.connect(self.ler_saida)
        p.finished.connect(self.terminou)
        p.errorOccurred.connect(self.erro_processo)
        self.processo = p
        self.travar(True)
        p.start(python_console(), [str(self.script)] + args + ["--progresso"])

    def travar(self, rodando: bool):
        for w in self.campos + [self.b_iniciar]:
            w.setEnabled(not rodando)
        self.b_cancelar.setEnabled(rodando)
        if rodando:
            for b, _ in self.botoes_saida:
                b.setEnabled(False)
        else:
            self.atualizar_botoes_saida()
        janela = self.window()
        if hasattr(janela, "abas"):
            janela.travar_abas(self if rodando else None)

    def ler_saida(self):
        if self.processo is None:
            return
        self.buffer += bytes(self.processo.readAllStandardOutput()).decode("utf-8", "replace")
        *linhas, self.buffer = self.buffer.replace("\r\n", "\n").split("\n")
        for linha in linhas:
            self.tratar_linha(linha)

    def tratar_linha(self, linha: str):
        if linha.startswith("@@PROGRESSO|"):
            try:
                _, etapa, atual, total, texto = (linha.split("|", 4) + [""])[:5]
                atual, total = int(atual), int(total)
                if texto:
                    self.texto_atual = texto
                # a barra do Qt só aceita números até ~2,1 bilhões, e algumas etapas
                # informam o andamento em bytes (18 GB passam disso): usa escala 0-1000
                self.barra.setRange(0, ESCALA_BARRA)
                self.barra.setValue(round(ESCALA_BARRA * min(atual, total) / total) if total > 0 else 0)
                self.etiqueta.setText(self.texto_progresso(etapa, atual, total, self.texto_atual))
            except (ValueError, OverflowError, ZeroDivisionError):
                pass        # linha de progresso malformada nunca deve atrapalhar
            return
        if linha.startswith("@@RESUMO|"):
            try:
                self.resumo = json.loads(linha[len("@@RESUMO|"):])
            except ValueError:
                pass
            return
        self.log.appendPlainText(linha)
        self.log.moveCursor(QTextCursor.End)

    def erro_processo(self, erro):
        if erro == QProcess.FailedToStart:
            self.log.appendPlainText(f"Não foi possível iniciar o Python: {python_console()}")

    def cancelar(self, perguntar=True):
        if self.processo is None:
            return
        if perguntar:
            r = QMessageBox.question(self, TITULO, "Interromper agora?\n\n" + self.aviso_cancelar)
            if r != QMessageBox.Yes or self.processo is None:
                return
        self.cancelado = True
        self.etiqueta.setText("Cancelando…")
        self.processo.kill()

    aviso_cancelar = ""

    def terminou(self, codigo, status):
        if self.buffer:
            self.tratar_linha(self.buffer)
            self.buffer = ""
        self.processo.deleteLater()
        self.processo = None
        self.travar(False)
        self.barra.setRange(0, 1)
        if self.cancelado:
            self.barra.setValue(0)
            self.etiqueta.setText("Cancelado. " + self.aviso_cancelar)
            return
        if codigo != 0 or status != QProcess.NormalExit or self.resumo is None:
            self.barra.setValue(0)
            self.etiqueta.setText("Terminou com erro. Veja o registro abaixo.")
            QMessageBox.warning(self, TITULO,
                                "O processamento não terminou. A mensagem de erro "
                                "está no registro, na parte de baixo da janela.")
            return
        self.barra.setValue(1)
        seg = int(time.monotonic() - self.inicio)
        duracao = f"{seg // 60} min {seg % 60} s" if seg >= 60 else f"{seg} s"
        self.concluido(self.resumo, duracao)

    def mostrar_final(self, texto: str, alerta: bool):
        caixa = QMessageBox(QMessageBox.Warning if alerta else QMessageBox.Information,
                            TITULO, texto, parent=self)
        b_pasta = caixa.addButton("Abrir pasta de saída", QMessageBox.ActionRole)
        caixa.addButton(QMessageBox.Close)
        caixa.exec()
        if caixa.clickedButton() is b_pasta:
            self.abrir(self.pasta_saida())

    def parar(self):
        if self.processo is not None:
            self.cancelado = True
            self.processo.kill()
            self.processo.waitForFinished(5000)
        for t in list(self.threads):
            t.requestInterruption()
            t.wait(3000)


# --------------------------------------------------------------------------
# Aba 1: separar em lotes
# --------------------------------------------------------------------------
class PainelSeparar(PainelExecucao):
    script = PASTA_PROGRAMA / "organizar_pdfs.py"
    aviso_cancelar = ("Ao iniciar de novo, os PDFs já particionados são reaproveitados.")

    def __init__(self, cfg):
        super().__init__(cfg)
        self.tamanhos = []

        intro = QLabel(
            "Os PDFs são processados em <b>ordem cronológica estrita</b> (número da medição, "
            "data ou lista definida) e empacotados em <b>.zip de até 48 MB</b> "
            "(48 × 1024 × 1024 bytes), sem compressão: cada PDF sai do .zip intacto, bit a "
            "bit. O tamanho real de cada .zip é medido; se o próximo PDF faria passar de "
            "48 MB, o pacote fecha e ele abre o seguinte (Pacote_01.zip, Pacote_02.zip…). "
            "PDFs maiores que o teto são particionados em volumes sem perda. Saída na "
            "subpasta <b>Pacotes_Agrupados</b>; os originais não são alterados.")
        intro.setWordWrap(True)
        self.corpo.addWidget(intro)

        caixa = QGroupBox("Pastas")
        grade = QGridLayout(caixa)
        self.entrada = CampoPasta("Pasta com os PDFs originais")
        self.saida = CampoPasta("Pasta de saída")
        self.saida.texto.setPlaceholderText(
            "Vazio = na própria pasta dos PDFs (subpasta Pacotes_Agrupados)")
        grade.addWidget(QLabel("PDFs originais:"), 0, 0)
        grade.addWidget(self.entrada, 0, 1)
        grade.addWidget(QLabel("Saída:"), 1, 0)
        grade.addWidget(self.saida, 1, 1)
        self.info = QLabel(" ")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: palette(placeholder-text);")
        grade.addWidget(self.info, 2, 1)
        grade.setColumnStretch(1, 1)
        self.corpo.addWidget(caixa)

        caixa_op = QGroupBox("Opções")
        op = QGridLayout(caixa_op)
        self.meta = QDoubleSpinBox()
        self.meta.setRange(1, 2000)
        self.meta.setDecimals(2)
        self.meta.setSingleStep(0.5)
        self.meta.setSuffix(" MB")
        self.meta.setValue(48)
        self.meta.setToolTip("Tamanho máximo de cada pacote, conferido no arquivo gravado.")
        op.addWidget(QLabel("Teto de cada pacote:"), 0, 0)
        op.addWidget(self.meta, 0, 1)
        self.mb_windows = QCheckBox("Contar MB como o Windows (1 MB = 1.048.576 bytes)")
        self.mb_windows.setToolTip(
            "Desmarcado: 1 MB = 1.000.000 bytes, e 47,5 MB = 47.500.000 bytes, que fica abaixo "
            "de 48 MB em qualquer forma de contar. Marque só se tiver certeza de que o "
            "sistema do tribunal conta como o Windows.")
        op.addWidget(self.mb_windows, 0, 2)
        self.formato = QComboBox()
        self.formato.addItem(".zip com os PDFs intactos (sem compressão, bit a bit)", "zip")
        self.formato.addItem("PDF único (páginas unidas)", "pdf")
        op.addWidget(QLabel("Formato do pacote:"), 1, 0)
        op.addWidget(self.formato, 1, 1, 1, 2)
        self.ordem = QComboBox()
        self.ordem.addItem("Número da medição no nome (sem número: no fim, por data de criação)",
                           "medicao")
        self.ordem.addItem("Ordem definida em lista (.txt)", "lista")
        self.ordem.addItem("Natural pelo nome (15 antes de 20, Parte_1 antes de Parte_2)", "natural")
        self.ordem.addItem("Data de criação registrada no PDF (se não houver, a do arquivo)", "criacao")
        self.ordem.addItem("Data de criação do arquivo (como no Windows)", "criacao-arquivo")
        self.ordem.addItem("Data de modificação", "data")
        self.ordem.setToolTip(
            "Data de criação registrada no PDF: gravada pelo programa que gerou o documento; "
            "não muda quando o arquivo é copiado.\n"
            "Data de criação do arquivo: a do Windows; muda quando o arquivo é copiado ou baixado.\n"
            "O índice mostra a data usada e de onde ela veio.")
        op.addWidget(QLabel("Ordem:"), 2, 0)
        op.addWidget(self.ordem, 2, 1, 1, 2)
        self.lista = QLineEdit()
        self.lista.setPlaceholderText("Arquivo .txt com um nome de PDF por linha, na ordem desejada")
        self.b_lista = QPushButton("Escolher…")
        self.b_lista.clicked.connect(self.escolher_lista)
        self.linha_lista = QWidget()
        ll = QHBoxLayout(self.linha_lista)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Lista:"))
        ll.addWidget(self.lista, 1)
        ll.addWidget(self.b_lista)
        op.addWidget(self.linha_lista, 3, 1, 1, 2)
        self.ordem.currentIndexChanged.connect(self.ajustar_opcoes)
        self.por_pasta = QCheckBox("Não misturar subpastas no mesmo pacote")
        self.compactar = QCheckBox("Priorizar o menor número de pacotes "
                                   "(só ZIP; os pacotes deixam de seguir a ordem)")
        self.manter = QCheckBox("Guardar também os volumes particionados fora dos pacotes")
        self.assinados = QCheckBox("Particionar também PDFs com assinatura digital "
                                   "(a assinatura deixa de valer)")
        self.assinados.setToolTip("Sem esta opção, PDFs assinados acima do teto não são "
                                  "particionados e vão para a lista de exceções.")
        for k, w in enumerate((self.por_pasta, self.compactar, self.manter, self.assinados), 4):
            op.addWidget(w, k, 0, 1, 3)
        op.setColumnStretch(2, 1)
        self.corpo.addWidget(caixa_op)

        self.botao_saida("Abrir pacotes", "Pacotes_Agrupados")
        self.botao_saida("Abrir índice", "indice.csv")
        self.botao_saida("Abrir exceções", "excecoes.csv")
        self.campos = [self.entrada, self.saida, self.meta, self.mb_windows, self.formato,
                       self.ordem, self.lista, self.b_lista, self.por_pasta, self.compactar,
                       self.manter, self.assinados]

        self.entrada.alterado.connect(
            lambda: self.contar(self.entrada.valor(), self.saida.valor(), ".pdf", self.info,
                                ("_trabalho", "pacotes_agrupados", "excecoes", "lotes")))
        self.entrada.alterado.connect(self.atualizar_botoes_saida)
        self.saida.alterado.connect(self.atualizar_botoes_saida)
        self.meta.valueChanged.connect(self.mostrar_info)
        self.mb_windows.toggled.connect(self.mostrar_info)
        self.formato.currentIndexChanged.connect(self.ajustar_opcoes)

        c = self.cfg
        self.meta.setValue(float(c.value("separar3/teto", 48)))
        self.mb_windows.setChecked(c.value("separar3/mb_windows", "true") == "true")
        self.formato.setCurrentIndex(max(0, self.formato.findData(c.value("separar3/formato", "zip"))))
        self.ordem.setCurrentIndex(max(0, self.ordem.findData(c.value("separar3/ordem", "medicao"))))
        self.lista.setText(c.value("separar3/lista", ""))
        self.por_pasta.setChecked(c.value("separar3/por_pasta", "false") == "true")
        self.compactar.setChecked(c.value("separar3/compactar", "false") == "true")
        self.manter.setChecked(c.value("separar3/manter", "false") == "true")
        self.assinados.setChecked(c.value("separar3/assinados", "false") == "true")
        self.saida.definir(c.value("separar/saida", ""))
        self.entrada.definir(c.value("separar/entrada", ""))
        self.ajustar_opcoes()

    def ajustar_opcoes(self):
        zip_ = self.formato.currentData() == "zip"
        self.compactar.setEnabled(zip_ and self.processo is None)
        if not zip_:
            self.compactar.setChecked(False)
        self.linha_lista.setVisible(self.ordem.currentData() == "lista")

    def escolher_lista(self):
        inicio = os.path.dirname(self.lista.text()) or self.entrada.valor() or str(Path.home())
        arq, _ = QFileDialog.getOpenFileName(self, "Lista com a ordem dos PDFs", inicio,
                                             "Texto (*.txt *.csv);;Todos (*)")
        if arq:
            self.lista.setText(os.path.normpath(arq))

    def travar(self, rodando: bool):
        super().travar(rodando)
        if not rodando:
            self.ajustar_opcoes()

    def pasta_saida(self) -> str:
        return self.saida.valor() or self.entrada.valor()

    def unidade(self) -> int:
        return MIB if self.mb_windows.isChecked() else 1_000_000

    def info_pronta(self, tamanhos):
        self.tamanhos = tamanhos
        if not tamanhos:
            self.info.setText("Nenhum PDF nesta pasta.")
        self.mostrar_info()

    def mostrar_info(self):
        if not self.tamanhos:
            return
        meta = int(self.meta.value() * self.unidade())
        total = sum(self.tamanhos)
        grandes = sum(1 for t in self.tamanhos if t > meta - 128 * 1024)
        minimo = math.ceil(total / (meta - 128 * 1024))
        self.info.setText(
            f"{fmt_int(len(self.tamanhos))} PDFs · {fmt_gb(total)} · "
            f"{fmt_int(grandes)} acima do teto serão particionados · "
            f"mínimo de ~{fmt_int(minimo)} pacotes")

    def montar_argumentos(self):
        entrada, saida = self.entrada.valor(), self.saida.valor()
        if not entrada or not os.path.isdir(entrada):
            QMessageBox.warning(self, TITULO, "Escolha a pasta com os PDFs originais.")
            return None
        if not self.script.exists():
            QMessageBox.critical(self, TITULO, f"Não encontrei o arquivo {self.script.name} em:"
                                               f"\n{PASTA_PROGRAMA}")
            return None
        pac = Path(saida or entrada, "Pacotes_Agrupados")
        if pac.is_dir() and any(pac.glob("Pacote_*")):
            r = QMessageBox.question(
                self, TITULO, "A pasta Pacotes_Agrupados já tem pacotes de uma execução "
                              "anterior.\nEles serão substituídos pelos novos. Continuar?")
            if r != QMessageBox.Yes:
                return None
        lista = self.lista.text().strip().strip('"')
        if self.ordem.currentData() == "lista" and not os.path.isfile(lista):
            QMessageBox.warning(self, TITULO, "Escolha o arquivo .txt com a ordem definida.")
            return None
        c = self.cfg
        c.setValue("separar/entrada", entrada)
        c.setValue("separar/saida", saida)
        c.setValue("separar3/teto", self.meta.value())
        c.setValue("separar3/formato", self.formato.currentData())
        c.setValue("separar3/ordem", self.ordem.currentData())
        c.setValue("separar3/lista", lista)
        for nome, w in (("por_pasta", self.por_pasta), ("compactar", self.compactar),
                        ("manter", self.manter), ("assinados", self.assinados),
                        ("mb_windows", self.mb_windows)):
            c.setValue(f"separar3/{nome}", "true" if w.isChecked() else "false")
        args = [entrada] + ([saida] if saida else []) + [
            "--meta-mb", f"{self.meta.value():g}",
            "--unidade", "windows" if self.mb_windows.isChecked() else "decimal",
            "--formato", self.formato.currentData(), "--ordem", self.ordem.currentData()]
        if self.ordem.currentData() == "lista":
            args += ["--lista", lista]
        for w, op in ((self.por_pasta, "--por-pasta"),
                      (self.compactar, "--compactar"), (self.manter, "--manter-partes"),
                      (self.assinados, "--cortar-assinados")):
            if w.isChecked():
                args.append(op)
        return args

    def texto_progresso(self, etapa, atual, total, texto):
        if etapa == "corte":
            if atual < total:
                return f"Regra 1 – particionando ({atual + 1} de {total}): {texto}"
            return "Regra 2 – agrupando…"
        if etapa == "pacotes":
            return f"Regra 2 – agrupando: {texto} ({atual / max(total, 1):.0%})"
        if etapa == "datas":
            return f"Definindo a ordem: {atual} de {total}"
        return f"Regra 2 – gravando pacotes: {atual} de {total}"

    def concluido(self, r, duracao):
        self.etiqueta.setText(f"Concluído: {fmt_int(r['lotes'])} pacotes em {duracao}.")
        maior = f"{r['maior_mb']:.2f}".replace(".", ",")
        limite = f"{r['meta_mb']:g}".replace(".", ",")
        formato = "PDF único" if r.get("formato") == "pdf" else ".zip"
        ordem = {"medicao": "pelo número da medição", "lista": "definida em lista",
                 "natural": "natural pelo nome", "data": "por data de modificação",
                 "criacao": "por data de criação registrada no PDF",
                 "criacao-arquivo": "por data de criação do arquivo"}.get(r.get("ordem"), "")
        msg = (f"{fmt_int(r['lotes'])} pacotes ({formato}) em Pacotes_Agrupados, "
               f"ordem {ordem}.\n"
               f"Maior pacote: {maior} MB (teto {limite} MB).\n"
               f"PDFs lidos: {fmt_int(r['pdfs'])}; particionados: {fmt_int(r['cortados'])} "
               f"(em {fmt_int(r['partes'])} volumes).")
        if r.get("faixas"):
            msg += (f"\n{fmt_int(r['faixas'])} página(s) gigante(s) dividida(s) em faixas, "
                    "sem perda (a aba Remontar junta de volta).")
        if r.get("assinados"):
            msg += (f"\n{fmt_int(r['assinados'])} PDF(s) assinado(s) digitalmente não foram "
                    "particionados (estão nas exceções).")
        if r.get("travados"):
            msg += f"\n{fmt_int(r['travados'])} arquivo(s) barrado(s) pela trava de excesso."
        if r["excecoes"]:
            msg += (f"\n\n{fmt_int(r['excecoes'])} arquivo(s) não entraram nos pacotes. "
                    "Veja excecoes.csv.")
        if r["estouros"]:
            msg += "\n\nATENÇÃO: pacotes acima do teto: " + ", ".join(r["estouros"])
        self.mostrar_final(msg, bool(r["excecoes"] or r["estouros"]))


# --------------------------------------------------------------------------
# Aba 2: remontar os PDFs
# --------------------------------------------------------------------------
class PainelRemontar(PainelExecucao):
    script = PASTA_PROGRAMA / "remontar_pdfs.py"
    aviso_cancelar = "Os PDFs já remontados ficam na pasta de saída."

    def __init__(self, cfg):
        super().__init__(cfg)
        intro = QLabel(
            "Lê os pacotes (PDF único ou ZIP), <b>separa cada documento</b> e reconstrói "
            "cada PDF original, juntando os volumes na ordem (parte 1, 2, 3…), sem "
            "recomprimir. Título, sumário, links e data de modificação voltam como eram. "
            "<b>No fim, os PDFs restaurados são zipados</b> em pacotes .zip de até 48 MB "
            "(Pacote_01.zip, Pacote_02.zip…), com as mesmas regras de ordem e transbordo da "
            "aba 1; os que sozinhos passam do teto são divididos em volumes sem perda para "
            "caber. Escolha a pasta Pacotes_Agrupados (ou a pasta que a contém).")
        intro.setWordWrap(True)
        self.corpo.addWidget(intro)

        caixa = QGroupBox("Pastas")
        grade = QGridLayout(caixa)
        self.entrada = CampoPasta("Pasta com os pacotes")
        self.saida = CampoPasta("Pasta onde os PDFs remontados serão gravados")
        grade.addWidget(QLabel("Pacotes:"), 0, 0)
        grade.addWidget(self.entrada, 0, 1)
        grade.addWidget(QLabel("Saída (PDFs):"), 1, 0)
        grade.addWidget(self.saida, 1, 1)
        self.info = QLabel(" ")
        self.info.setStyleSheet("color: palette(placeholder-text);")
        grade.addWidget(self.info, 2, 1)
        grade.setColumnStretch(1, 1)
        self.corpo.addWidget(caixa)

        caixa_op = QGroupBox("Opções")
        op = QGridLayout(caixa_op)
        self.so_cortados = QCheckBox("Remontar só os PDFs que estavam em partes "
                                     "(não extrair os que vieram inteiros)")
        op.addWidget(self.so_cortados, 0, 0, 1, 3)
        self.zipar = QCheckBox("Zipar o resultado em pacotes .zip de até")
        self.meta = QDoubleSpinBox()
        self.meta.setRange(1, 2000)
        self.meta.setDecimals(2)
        self.meta.setSuffix(" MB")
        self.meta.setValue(48)
        self.mb_windows = QCheckBox("MB do Windows (1 MB = 1.048.576 bytes)")
        op.addWidget(self.zipar, 1, 0)
        op.addWidget(self.meta, 1, 1)
        op.addWidget(self.mb_windows, 1, 2)
        self.ordem = QComboBox()
        for texto, dado in (("Número da medição no nome (sem número: no fim, por data)", "medicao"),
                            ("Ordem definida em lista (.txt)", "lista"),
                            ("Natural pelo nome", "natural"),
                            ("Data de criação registrada no PDF", "criacao"),
                            ("Data de criação do arquivo", "criacao-arquivo"),
                            ("Data de modificação", "data")):
            self.ordem.addItem(texto, dado)
        op.addWidget(QLabel("Ordem nos .zip:"), 2, 0)
        op.addWidget(self.ordem, 2, 1, 1, 2)
        self.lista = QLineEdit()
        self.lista.setPlaceholderText("Arquivo .txt com um nome de PDF por linha")
        self.b_lista = QPushButton("Escolher…")
        self.b_lista.clicked.connect(self.escolher_lista)
        self.linha_lista = QWidget()
        ll = QHBoxLayout(self.linha_lista)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Lista:"))
        ll.addWidget(self.lista, 1)
        ll.addWidget(self.b_lista)
        op.addWidget(self.linha_lista, 3, 1, 1, 2)
        self.dividir = QCheckBox("Dividir em volumes, sem perda, os PDFs maiores que o teto "
                                 "(para que tudo caiba nos .zip)")
        self.assinados = QCheckBox("Dividir também PDFs com assinatura digital "
                                   "(a assinatura deixa de valer)")
        self.so_zip = QCheckBox("Manter só os .zip (apagar os PDFs soltos que foram zipados, "
                                "depois de conferir os .zip)")
        op.addWidget(self.dividir, 4, 0, 1, 3)
        op.addWidget(self.assinados, 5, 0, 1, 3)
        op.addWidget(self.so_zip, 6, 0, 1, 3)
        op.setColumnStretch(2, 1)
        self.corpo.addWidget(caixa_op)

        self.botao_saida("Abrir pasta de saída", "")
        self.botao_saida("Abrir pacotes .zip", "Pacotes_Agrupados")
        self.botao_saida("Abrir relatório", "relatorio_remontagem.csv")
        self.campos = [self.entrada, self.saida, self.so_cortados, self.zipar, self.meta,
                       self.mb_windows, self.ordem, self.lista, self.b_lista, self.dividir,
                       self.assinados, self.so_zip]
        self.dividir.toggled.connect(self.ajustar_opcoes)
        self.zipar.toggled.connect(self.ajustar_opcoes)
        self.ordem.currentIndexChanged.connect(self.ajustar_opcoes)

        self.entrada.alterado.connect(
            lambda: self.contar(self.entrada.valor(), self.saida.valor(), (".pdf", ".zip"),
                                self.info))
        self.saida.alterado.connect(self.atualizar_botoes_saida)

        c = self.cfg
        self.so_cortados.setChecked(c.value("remontar/so_cortados", "false") == "true")
        self.zipar.setChecked(c.value("remontar2/zipar", "true") == "true")
        self.meta.setValue(float(c.value("remontar2/teto", 48)))
        self.mb_windows.setChecked(c.value("remontar2/mb_windows", "true") == "true")
        self.ordem.setCurrentIndex(max(0, self.ordem.findData(c.value("remontar2/ordem", "medicao"))))
        self.lista.setText(c.value("remontar2/lista", ""))
        self.so_zip.setChecked(c.value("remontar2/so_zip", "false") == "true")
        self.dividir.setChecked(c.value("remontar2/dividir", "true") == "true")
        self.assinados.setChecked(c.value("remontar2/assinados", "false") == "true")
        self.saida.definir(c.value("remontar/saida", ""))
        self.entrada.definir(c.value("remontar/entrada", ""))
        self.ajustar_opcoes()

    def ajustar_opcoes(self):
        livre = self.processo is None
        z = self.zipar.isChecked()
        for w in (self.meta, self.mb_windows, self.ordem, self.so_zip, self.lista, self.b_lista,
                  self.dividir):
            w.setEnabled(z and livre)
        self.assinados.setEnabled(z and livre and self.dividir.isChecked())
        self.linha_lista.setVisible(self.ordem.currentData() == "lista")

    def travar(self, rodando: bool):
        super().travar(rodando)
        if not rodando:
            self.ajustar_opcoes()

    def escolher_lista(self):
        inicio = os.path.dirname(self.lista.text()) or self.entrada.valor() or str(Path.home())
        arq, _ = QFileDialog.getOpenFileName(self, "Lista com a ordem dos PDFs", inicio,
                                             "Texto (*.txt *.csv);;Todos (*)")
        if arq:
            self.lista.setText(os.path.normpath(arq))

    def info_pronta(self, tamanhos):
        if tamanhos:
            self.info.setText(f"{fmt_int(len(tamanhos))} arquivos PDF/ZIP · {fmt_gb(sum(tamanhos))}")
        else:
            self.info.setText("Nenhum pacote (PDF ou ZIP) nesta pasta.")

    def montar_argumentos(self):
        entrada, saida = self.entrada.valor(), self.saida.valor()
        if not self.validar_pastas(entrada, saida, "com os pacotes"):
            return None
        if os.path.isdir(saida) and any(p.suffix.lower() == ".pdf"
                                        for p in Path(saida).rglob("*.pdf")):
            r = QMessageBox.question(
                self, TITULO, "A pasta de saída já tem PDFs. Os que tiverem o mesmo nome "
                              "serão substituídos. Continuar?")
            if r != QMessageBox.Yes:
                return None
        lista = self.lista.text().strip().strip('"')
        if self.zipar.isChecked() and self.ordem.currentData() == "lista" \
                and not os.path.isfile(lista):
            QMessageBox.warning(self, TITULO, "Escolha o arquivo .txt com a ordem definida.")
            return None
        c = self.cfg
        c.setValue("remontar/entrada", entrada)
        c.setValue("remontar/saida", saida)
        c.setValue("remontar/so_cortados", "true" if self.so_cortados.isChecked() else "false")
        for nome, w in (("zipar", self.zipar), ("mb_windows", self.mb_windows),
                        ("so_zip", self.so_zip), ("dividir", self.dividir),
                        ("assinados", self.assinados)):
            c.setValue(f"remontar2/{nome}", "true" if w.isChecked() else "false")
        c.setValue("remontar2/teto", self.meta.value())
        c.setValue("remontar2/ordem", self.ordem.currentData())
        c.setValue("remontar2/lista", lista)
        args = [entrada, saida]
        if self.so_cortados.isChecked():
            args.append("--so-cortados")
        if self.zipar.isChecked():
            args += ["--zipar", "--meta-mb", f"{self.meta.value():g}",
                     "--unidade", "windows" if self.mb_windows.isChecked() else "decimal",
                     "--ordem", self.ordem.currentData()]
            if self.ordem.currentData() == "lista":
                args += ["--lista", lista]
            if self.so_zip.isChecked():
                args.append("--so-zip")
            if not self.dividir.isChecked():
                args.append("--nao-dividir")
            elif self.assinados.isChecked():
                args.append("--cortar-assinados")
        return args

    def texto_progresso(self, etapa, atual, total, texto):
        if etapa == "leitura":
            return f"Lendo os pacotes ZIP: {atual} de {total}"
        if etapa == "pacotes":
            return f"Separando os documentos dos pacotes: {atual} de {total}"
        if etapa == "zip":
            return f"Zipando o resultado: {texto} ({atual / max(total, 1):.0%})"
        if etapa == "volumes":
            return (f"Dividindo em volumes os maiores que o teto ({atual + 1} de {total}): {texto}"
                    if atual < total else "Zipando o resultado…")
        if etapa == "datas":
            return f"Definindo a ordem dos .zip: {atual} de {total}"
        if atual < total:
            return f"Remontando ({atual + 1} de {total}): {texto}"
        return "Gravando o relatório…"

    def concluido(self, r, duracao):
        restaurados = r["remontados"] + r["extraidos"]
        self.etiqueta.setText(f"Concluído: {fmt_int(restaurados)} PDFs restaurados"
                              + (f" e zipados em {fmt_int(r['zips'])} .zip" if "zips" in r else "")
                              + f" em {duracao}.")
        msg = (f"{fmt_int(r['documentos'])} documentos processados.\n"
               f"Remontados a partir das partes: {fmt_int(r['remontados'])}\n"
               f"Extraídos inteiros: {fmt_int(r['extraidos'])}")
        alerta = False
        if "zips" in r:
            maior = f"{r['maior_zip_mb']:.2f}".replace(".", ",")
            msg += (f"\n\nZipados em {fmt_int(r['zips'])} pacote(s) .zip na pasta "
                    f"Pacotes_Agrupados (maior: {maior} MB; teto {r['meta_mb']:g} MB).")
            if r.get("divididos"):
                msg += (f"\n{fmt_int(r['divididos'])} PDF(s) maiores que o teto foram divididos "
                        f"sem perda em {fmt_int(r['volumes'])} volumes, dentro dos .zip.")
            if r["fora_zip"]:
                alerta = True
                msg += (f"\n{fmt_int(r['fora_zip'])} PDF(s) não puderam entrar nos .zip e "
                        "ficaram soltos na pasta (veja o motivo no relatório).")
            if r.get("estouros"):
                alerta = True
                msg += "\nATENÇÃO: .zip acima do teto: " + ", ".join(r["estouros"])
        if r["incompletos"]:
            alerta = True
            msg += (f"\n\n{fmt_int(r['incompletos'])} documento(s) incompleto(s): falta "
                    "alguma parte. As partes encontradas estão na pasta _INCOMPLETOS.")
        if r["erros"]:
            alerta = True
            msg += f"\n\n{fmt_int(r['erros'])} conflito(s) ou erro(s)."
        if alerta:
            msg += "\n\nDetalhes em relatorio_remontagem.csv."
        self.mostrar_final(msg, alerta)


# --------------------------------------------------------------------------
# Aba 3: testar neste computador
# --------------------------------------------------------------------------
class PainelTeste(PainelExecucao):
    script = PASTA_PROGRAMA / "autoteste.py"
    aviso_cancelar = "Nenhum arquivo seu foi alterado."

    def __init__(self, cfg):
        super().__init__(cfg)
        intro = QLabel(
            "Testa o programa <b>neste computador</b>, do começo ao fim, sem tocar nos seus "
            "arquivos. Cria PDFs de teste numa pasta temporária (cerca de 70 MB) com os "
            "casos difíceis: datas variadas, sumário e links entre partes, página gigante "
            "com imagem sem perda, página gigante em JPEG e PDF assinado. Depois separa "
            "em lotes, remonta e compara cada PDF com o original: páginas, pixels das "
            "imagens, sumário, links e datas.<br><br>Leva menos de um minuto. "
            "Faça isso uma vez antes de processar os arquivos de verdade.")
        intro.setWordWrap(True)
        self.corpo.addWidget(intro)
        self.b_iniciar.setText("Testar agora")
        self.saida = CampoPasta("")      # não usado; mantém a interface comum

    def montar_argumentos(self):
        if not self.script.exists():
            QMessageBox.critical(self, TITULO, f"Não encontrei o arquivo {self.script.name} "
                                               f"em:\n{PASTA_PROGRAMA}")
            return None
        return []

    def texto_progresso(self, etapa, atual, total, texto):
        return f"Etapa {min(atual + 1, total)} de {total}: {texto}" if atual < total \
            else "Finalizando…"

    def terminou(self, codigo, status):
        # o autoteste sai com código 1 quando encontra falha: trata como resultado
        if self.resumo is None and self.buffer:
            self.tratar_linha(self.buffer)
            self.buffer = ""
        if self.resumo is not None and not self.cancelado:
            codigo = 0
        super().terminou(codigo, status)

    def concluido(self, r, duracao):
        if r["ok"]:
            self.etiqueta.setText(f"TUDO CERTO: {r['verificacoes']} verificações em {duracao}.")
            QMessageBox.information(self, TITULO,
                                    f"Teste concluído: {r['verificacoes']} verificações, "
                                    "nenhuma falha.\n\nO programa funciona neste computador.")
        else:
            self.etiqueta.setText(f"HÁ FALHAS: {len(r['falhas'])} de {r['verificacoes']} "
                                  "verificações. Veja o registro.")
            QMessageBox.warning(self, TITULO,
                                "O teste encontrou falhas:\n\n" + "\n".join(r["falhas"][:8]) +
                                f"\n\nOs arquivos do teste ficaram em:\n{r['pasta']}")


# --------------------------------------------------------------------------
# Janela
# --------------------------------------------------------------------------
class Janela(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(TITULO)
        self.cfg = QSettings("OrganizadorPDFs", "OrganizadorPDFs")
        self.abas = QTabWidget()
        self.separar = PainelSeparar(self.cfg)
        self.remontar = PainelRemontar(self.cfg)
        self.teste = PainelTeste(self.cfg)
        self.abas.addTab(self.separar, "1. Separar em lotes")
        self.abas.addTab(self.remontar, "2. Remontar PDFs")
        self.abas.addTab(self.teste, "3. Testar neste computador")
        self.setCentralWidget(self.abas)
        self.abas.setCurrentIndex(int(self.cfg.value("aba", 0)))
        self.abas.currentChanged.connect(lambda i: self.cfg.setValue("aba", i))
        self.resize(860, 700)

    def paineis(self):
        return [self.separar, self.remontar, self.teste]

    def travar_abas(self, ativo):
        """Enquanto uma aba roda, a outra fica bloqueada."""
        for i, p in enumerate(self.paineis()):
            self.abas.setTabEnabled(i, ativo is None or p is ativo)

    def closeEvent(self, ev):
        if any(p.processo is not None for p in self.paineis()):
            r = QMessageBox.question(self, TITULO,
                                     "O processamento ainda está rodando. Interromper e sair?")
            if r != QMessageBox.Yes:
                ev.ignore()
                return
        for p in self.paineis():
            p.parar()
        ev.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(TITULO)
    janela = Janela()
    janela.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

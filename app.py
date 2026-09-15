# -*- coding: utf-8 -*-
"""
Decupagem de vídeos do YouTube
==============================
Interface Streamlit que recebe o link de um vídeo do YouTube, extrai o áudio,
transcreve com marcação de tempo e exporta tudo para um arquivo .docx.

Execução:  streamlit run app.py
"""
from __future__ import annotations

import datetime as dt
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import streamlit as st

APP_TITLE = "Decupagem de vídeos do YouTube"
WORKDIR = Path(tempfile.gettempdir()) / "decupagem_youtube"
WORKDIR.mkdir(parents=True, exist_ok=True)

# O Streamlit Community Cloud monta o repositório em /mount/src. Saber disso muda
# o que faz sentido oferecer: lá não há navegador de onde tirar cookies, e o
# YouTube trata o IP do datacenter com muito mais desconfiança.
NA_NUVEM = Path("/mount/src").exists()


# --------------------------------------------------------------------------- #
# Certificados TLS
#
# Em rede corporativa o tráfego HTTPS costuma ser inspecionado por um proxy que
# reemite os certificados assinando com uma autoridade própria. Essa autoridade
# está instalada no Windows, mas não no pacote do certifi — que é o único que o
# yt-dlp consulta. O resultado é o erro CERTIFICATE_VERIFY_FAILED
# ("unable to get local issuer certificate").
#
# A solução é montar um pacote que junte os certificados públicos do certifi com
# os que estão instalados no Windows, e fazer o certifi apontar para ele.
# --------------------------------------------------------------------------- #
def aplicar_ca_bundle(caminho: str | Path) -> None:
    """Passa a validar o TLS pelo arquivo .pem indicado."""
    caminho = str(caminho)
    os.environ["SSL_CERT_FILE"] = caminho
    os.environ["REQUESTS_CA_BUNDLE"] = caminho
    try:
        import certifi

        # O yt-dlp chama certifi.where() a cada conexão e ignora SSL_CERT_FILE.
        certifi.where = lambda _c=caminho: _c
    except ImportError:
        pass


def montar_bundle_do_windows() -> tuple[Path | None, int]:
    """Gera um .pem com os certificados do certifi mais os do repositório do
    Windows. Devolve (arquivo, quantidade de certificados do Windows)."""
    import ssl

    if sys.platform != "win32" or not hasattr(ssl, "enum_certificates"):
        return None, 0

    pems: list[str] = []
    try:
        import certifi

        pems.append(Path(certifi.where()).read_text(encoding="utf-8"))
    except Exception:
        pass

    do_windows = 0
    for loja in ("ROOT", "CA"):
        try:
            certificados = ssl.enum_certificates(loja)
        except Exception:
            continue
        for der, codificacao, confianca in certificados:
            if codificacao != "x509_asn":
                continue
            # trust == True significa "confiar para qualquer uso"; caso contrário
            # vem o conjunto de OIDs, e queremos o de autenticação de servidor.
            if confianca is not True and "1.3.6.1.5.5.7.3.1" not in (confianca or ()):
                continue
            try:
                pems.append(ssl.DER_cert_to_PEM_cert(der))
                do_windows += 1
            except Exception:
                continue

    if not do_windows:
        return None, 0

    destino = WORKDIR / "ca-bundle-windows.pem"
    destino.write_text("\n".join(pems), encoding="utf-8")
    return destino, do_windows


def preparar_certificados() -> dict:
    """Configura a validação de TLS logo na abertura do app."""
    estado = {"truststore": False, "bundle": None, "certificados_windows": 0}

    # Caminho preferido: o truststore delega a verificação ao próprio Windows,
    # acompanhando qualquer certificado adicionado depois.
    try:
        import truststore

        truststore.inject_into_ssl()
        estado["truststore"] = True
    except Exception:
        pass

    # Rede de segurança, independente do truststore: o yt-dlp lê o pacote do
    # certifi diretamente, sem passar pelo contexto padrão do Python.
    try:
        bundle, quantidade = montar_bundle_do_windows()
        if bundle:
            aplicar_ca_bundle(bundle)
            estado["bundle"] = bundle
            estado["certificados_windows"] = quantidade
    except Exception:
        pass

    return estado


TLS = preparar_certificados()
CERTIFICADOS_DO_SISTEMA = TLS["truststore"] or bool(TLS["bundle"])


# --------------------------------------------------------------------------- #
# Estruturas de dados
# --------------------------------------------------------------------------- #
@dataclass
class Bloco:
    """Um trecho contíguo de fala, com o tempo em que começa e termina."""

    inicio: float
    fim: float
    texto: str


@dataclass
class Resultado:
    blocos: list[Bloco]
    meta: dict = field(default_factory=dict)
    idioma: str | None = None


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def hms(segundos: float, com_ms: bool = False) -> str:
    """Converte segundos em HH:MM:SS (opcionalmente com milissegundos)."""
    segundos = max(0.0, float(segundos))
    h, resto = divmod(int(segundos), 3600)
    m, s = divmod(resto, 60)
    base = f"{h:02d}:{m:02d}:{s:02d}"
    if com_ms:
        ms = int(round((segundos - int(segundos)) * 1000))
        return f"{base},{ms:03d}"
    return base


def achar_ffmpeg() -> str | None:
    """ffmpeg do sistema; se não houver, o binário empacotado pelo imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def slug(texto: str, limite: int = 60) -> str:
    texto = re.sub(r"[^\w\s-]", "", texto, flags=re.UNICODE).strip()
    texto = re.sub(r"[\s_-]+", "-", texto)
    return (texto[:limite] or "decupagem").strip("-")


def valida_url(url: str) -> bool:
    return bool(re.match(r"https?://", url.strip(), flags=re.I))


def proxy_configurado() -> str:
    """Proxy para o yt-dlp, se houver um definido no ambiente ou nos secrets.

    É a única forma de baixar do YouTube a partir de um servidor: os IPs de
    datacenter são recusados, e sair por um proxy residencial contorna isso.
    Fica fora da interface de propósito — é configuração de implantação.
    """
    for chave in ("YTDLP_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        if valor := os.environ.get(chave):
            return valor
    try:
        return st.secrets.get("YTDLP_PROXY", "")
    except Exception:
        return ""


def _data_legivel(aaaammdd: str | None) -> str:
    if not aaaammdd or len(aaaammdd) != 8:
        return ""
    try:
        return dt.datetime.strptime(aaaammdd, "%Y%m%d").strftime("%d/%m/%Y")
    except ValueError:
        return ""


class _LoggerYtdlp:
    """Encaminha só o que interessa do yt-dlp para o painel de status."""

    def __init__(self, log: Callable[[str], None]):
        self._log = log

    def debug(self, msg):
        if msg and not msg.startswith("[debug]"):
            self._log(msg)

    def info(self, msg):
        self._log(msg)

    def warning(self, msg):
        self._log(f"Aviso: {msg}")

    def error(self, msg):
        self._log(f"Erro: {msg}")


# --------------------------------------------------------------------------- #
# Runtime JavaScript
#
# O YouTube embaralha as URLs de mídia com um desafio em JavaScript. Sem resolvê-lo
# as URLs vêm inválidas e o download morre em "HTTP Error 403". Quem resolve é um
# runtime externo, e o yt-dlp exige versões mínimas — o Node do apt do Debian, por
# exemplo, é antigo demais e acaba recusado em silêncio. Por isso aqui a versão é
# conferida antes, e o caminho vai explícito para o yt-dlp.
# --------------------------------------------------------------------------- #
VERSOES_MINIMAS_JS = {
    "deno": (2, 3, 0),
    "node": (22, 0, 0),
    "bun": (1, 2, 11),
}


def _versao_do_executavel(caminho: str) -> tuple[int, ...] | None:
    try:
        saida = subprocess.run(
            [caminho, "--version"], capture_output=True, text=True, timeout=20
        )
    except Exception:
        return None
    achado = re.search(r"(\d+)\.(\d+)\.(\d+)", (saida.stdout or "") + (saida.stderr or ""))
    return tuple(int(n) for n in achado.groups()) if achado else None


def _node_empacotado() -> str | None:
    """O Node instalado via pip (nodejs-wheel-binaries), que fica dentro do pacote
    em vez de entrar no PATH — é como garantimos uma versão recente no servidor."""
    try:
        import nodejs_wheel.executable as ne

        raiz = Path(ne.ROOT_DIR)
        caminho = raiz / "node.exe" if os.name == "nt" else raiz / "bin" / "node"
        return str(caminho) if caminho.exists() else None
    except Exception:
        return None


@st.cache_data(show_spinner=False, ttl=300)
def runtimes_js_suportados() -> tuple[dict, list[str]]:
    """Devolve ({runtime: {'path': ...}}, ['recusados por serem antigos'])."""
    candidatos: list[tuple[str, str]] = []
    if empacotado := _node_empacotado():
        candidatos.append(("node", empacotado))
    for nome, executavel in (("deno", "deno"), ("node", "node"), ("bun", "bun")):
        if achado := shutil.which(executavel):
            candidatos.append((nome, achado))

    runtimes: dict[str, dict] = {}
    recusados: dict[str, str] = {}
    for nome, caminho in candidatos:
        if nome in runtimes:
            continue  # já temos um binário bom para este runtime
        versao = _versao_do_executavel(caminho)
        if versao is None:
            continue
        minima = VERSOES_MINIMAS_JS[nome]
        if versao >= minima:
            runtimes[nome] = {"path": caminho}
            recusados.pop(nome, None)
        else:
            recusados.setdefault(
                nome,
                f"{nome} {'.'.join(map(str, versao))} "
                f"(o yt-dlp exige {'.'.join(map(str, minima))} ou mais novo)",
            )
    return runtimes, list(recusados.values())


# --------------------------------------------------------------------------- #
# 1) Download e extração do áudio
# --------------------------------------------------------------------------- #
# Cada "player client" do YouTube entrega as URLs de mídia sob regras próprias.
# Quando uma delas é recusada com 403, tentar outro cliente costuma resolver —
# é o contorno padrão para esse erro.
CLIENTES_YOUTUBE: tuple[tuple[str, ...] | None, ...] = (
    None,                 # a rotação que o próprio yt-dlp faz
    ("android_vr",),      # dispensa o desafio de JavaScript; o que mais funciona em servidor
    ("tv_simply",),
    ("web_safari",),
    ("ios",),
)

# Erros em que trocar de cliente não adianta: o vídeo simplesmente não está
# disponível para quem pede.
ERROS_DEFINITIVOS = (
    "video unavailable",
    "private video",
    "removed by the uploader",
    "unsupported url",
    "is not a valid url",
    "members-only",
    "this live event will begin",
)


# Abaixo disso o arquivo não tem áudio de verdade: é o resto de um download em
# que o YouTube recusou todos os fragmentos.
TAMANHO_MINIMO_AUDIO = 32 * 1024


def _arquivo_baixado(destino: Path, vid: str) -> Path | None:
    """O maior arquivo já finalizado do vídeo (ignora .part e afins)."""
    candidatos = sorted(
        (p for p in destino.glob(f"{vid}.*")
         if p.is_file() and p.suffix.lower() not in (".part", ".ytdl", ".temp")),
        key=lambda p: -p.stat().st_size,
    )
    return candidatos[0] if candidatos else None


def _limpar(destino: Path) -> None:
    for p in destino.iterdir():
        if p.is_file() and p.name != "cookies.txt":
            p.unlink(missing_ok=True)


def _extrair_com_rodizio(
    opts: dict, url: str, destino: Path, log: Callable[[str], None]
) -> tuple[Path, dict]:
    """Baixa tentando clientes diferentes do YouTube até um funcionar.

    Cada "player client" do YouTube entrega as URLs de mídia sob regras próprias:
    uns exigem que o desafio de JavaScript seja resolvido, outros não. Quando um
    é recusado com 403 ou não devolve formato nenhum, outro costuma passar.

    O download também é conferido depois de pronto: o YouTube às vezes aceita o
    pedido e recusa todos os fragmentos, o que deixaria um arquivo oco passar por
    bom.
    """
    import yt_dlp

    ultimo_erro: Exception | None = None
    for tentativa, clientes in enumerate(CLIENTES_YOUTUBE, start=1):
        opcoes = dict(opts)
        if clientes:
            extras = dict(opcoes.get("extractor_args") or {})
            extras["youtube"] = {**extras.get("youtube", {}), "player_client": list(clientes)}
            opcoes["extractor_args"] = extras
            log(f"Tentativa {tentativa} de {len(CLIENTES_YOUTUBE)}: "
                f"cliente {', '.join(clientes)}…")
        try:
            with yt_dlp.YoutubeDL(opcoes) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as exc:
            ultimo_erro = exc
            if any(marca in str(exc).lower() for marca in ERROS_DEFINITIVOS):
                raise
            log("Não deu certo; tentando outro cliente do YouTube…")
            _limpar(destino)
            continue

        arquivo = _arquivo_baixado(destino, info.get("id", "audio"))
        if arquivo is None or arquivo.stat().st_size < TAMANHO_MINIMO_AUDIO:
            ultimo_erro = RuntimeError(
                "O YouTube aceitou o pedido mas não entregou o conteúdo "
                "(fragmentos recusados)."
            )
            log("O arquivo veio vazio; tentando outro cliente do YouTube…")
            _limpar(destino)
            continue

        return arquivo, info

    raise ultimo_erro or RuntimeError("Não foi possível baixar o áudio do vídeo.")


def baixar_audio(
    url: str,
    destino: Path,
    cookies_browser: str | None = None,
    cookies_file: str | None = None,
    solver_remoto: bool = False,
    ca_bundle: str | None = None,
    ignorar_certificado: bool = False,
    log: Callable[[str], None] = lambda _m: None,
) -> tuple[Path, dict]:
    """Baixa a melhor trilha de áudio do vídeo e devolve (arquivo, metadados)."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError(
            "yt-dlp não está instalado. Rode:  pip install -r requirements.txt"
        ) from exc

    # Baixa o vídeo completo (não só a trilha de áudio) para uma pasta temporária;
    # o áudio é extraído dele a seguir e o vídeo é apagado ao final do processo em
    # `processar`. A conversão em si fica a cargo de converter_para_wav16k: o
    # binário do imageio-ffmpeg não se chama "ffmpeg.exe", e o yt-dlp só aceita
    # esse nome ao procurar o executável.
    opts: dict = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": str(destino / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        # Sem isto o yt-dlp pula os fragmentos recusados e termina "com sucesso",
        # entregando um arquivo sem áudio nenhum.
        "skip_unavailable_fragments": False,
        "logger": _LoggerYtdlp(log),
    }
    if proxy := proxy_configurado():
        opts["proxy"] = proxy
        # Não registramos o endereço: costuma trazer usuário e senha embutidos.
        log("Saindo por proxy configurado.")

    ffmpeg_sistema = shutil.which("ffmpeg")
    if ffmpeg_sistema:
        opts["ffmpeg_location"] = str(Path(ffmpeg_sistema).parent)

    runtimes, recusados = runtimes_js_suportados()
    if runtimes:
        opts["js_runtimes"] = runtimes
        log("Runtime JavaScript: "
            + ", ".join(f"{n} ({Path(c['path']).name})" for n, c in runtimes.items()))
    else:
        aviso = ("Sem runtime JavaScript utilizável: o YouTube não vai liberar os "
                 "formatos de áudio e o download tende a falhar com 403.")
        if recusados:
            aviso += " Encontrado, mas recusado pelo yt-dlp: " + "; ".join(recusados) + "."
        aviso += (" Instale o pacote 'nodejs-wheel-binaries' (já está no requirements.txt)"
                  " ou o Deno 2.3+.")
        log(aviso)
    if solver_remoto:
        opts["remote_components"] = ["ejs:github"]

    if ca_bundle:
        aplicar_ca_bundle(ca_bundle)
        log(f"Usando os certificados de {ca_bundle}.")
    elif CERTIFICADOS_DO_SISTEMA:
        log("Validando o TLS pelos certificados instalados no sistema.")
    if ignorar_certificado:
        opts["nocheckcertificate"] = True
        log("Atenção: a verificação do certificado TLS está desativada.")

    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    if cookies_file:
        opts["cookiefile"] = cookies_file

    arquivo, info = _extrair_com_rodizio(opts, url, destino, log)

    meta = {
        "titulo": info.get("title") or "(sem título)",
        "canal": info.get("uploader") or info.get("channel") or "",
        "duracao": float(info.get("duration") or 0.0),
        "url": info.get("webpage_url") or url,
        "publicado_em": _data_legivel(info.get("upload_date")),
        "id": info.get("id", "audio"),
    }
    log(f"Áudio obtido: {arquivo.name} ({arquivo.stat().st_size / 1e6:.1f} MB)")
    return arquivo, meta


def converter_para_wav16k(origem: Path, destino: Path) -> Path:
    """Normaliza qualquer áudio/vídeo para WAV 16 kHz mono."""
    if origem.suffix.lower() == ".wav":
        return origem

    ffmpeg = achar_ffmpeg()
    if ffmpeg:
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(origem),
             "-vn", "-ac", "1", "-ar", "16000", str(destino)],
            check=True,
            capture_output=True,
        )
        return destino

    # Sem binário de ffmpeg: usa o PyAV, que já vem com o faster-whisper.
    import av

    entrada = av.open(str(origem))
    saida = av.open(str(destino), mode="w")
    stream = saida.add_stream("pcm_s16le", rate=16000)
    stream.layout = "mono"
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
    for frame in entrada.decode(audio=0):
        frame.pts = None
        for quadro in resampler.resample(frame):
            for pacote in stream.encode(quadro):
                saida.mux(pacote)
    for pacote in stream.encode(None):
        saida.mux(pacote)
    saida.close()
    entrada.close()
    return destino


# --------------------------------------------------------------------------- #
# 2) Transcrição local — faster-whisper
# --------------------------------------------------------------------------- #
MODELO = "small"          # equilibrio entre qualidade e tempo em CPU
DISPOSITIVO = "cpu"
COMPUTE_TYPE = "int8"     # quantizacao que torna o modelo viavel em CPU
IDIOMA = "pt"


@st.cache_resource(show_spinner=False)
def carregar_whisper():
    from faster_whisper import WhisperModel

    return WhisperModel(MODELO, device=DISPOSITIVO, compute_type=COMPUTE_TYPE)


def transcrever_local(
    wav: Path,
    progresso: Callable[[float], None] = lambda _p: None,
    log: Callable[[str], None] = lambda _m: None,
) -> tuple[list[dict], str]:
    """Transcreve e devolve (lista de palavras com tempo, idioma)."""
    log(f"Carregando o modelo Whisper '{MODELO}' ({DISPOSITIVO}/{COMPUTE_TYPE})…")
    modelo_w = carregar_whisper()

    segmentos, info = modelo_w.transcribe(
        str(wav),
        language=IDIOMA,
        task="transcribe",
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    total = float(getattr(info, "duration", 0.0)) or 1.0

    palavras: list[dict] = []
    for seg in segmentos:
        if seg.words:
            for w in seg.words:
                if w.word.strip():
                    palavras.append(
                        {"inicio": float(w.start), "fim": float(w.end), "texto": w.word}
                    )
        elif seg.text.strip():
            palavras.append(
                {"inicio": float(seg.start), "fim": float(seg.end), "texto": seg.text}
            )
        progresso(min(1.0, float(seg.end) / total))
    progresso(1.0)
    return palavras, IDIOMA


# --------------------------------------------------------------------------- #
# 3) Montagem dos blocos de fala
# --------------------------------------------------------------------------- #
def montar_blocos(
    palavras: list[dict],
    pausa_maxima: float = 2.0,
    duracao_maxima: float = 40.0,
    duracao_limite: float = 75.0,
) -> list[Bloco]:
    """Agrupa palavras em blocos de fala.

    O corte acontece em pausas longas e, quando o bloco já está comprido, na
    primeira fronteira natural do texto — ponto final primeiro, vírgula depois.
    Acima de `duracao_limite` o corte é forçado, porque fala corrida sem
    pontuação renderia parágrafos intransponíveis no documento.
    """
    if not palavras:
        return []

    blocos: list[Bloco] = []
    atual: list[dict] = []

    def fecha():
        if not atual:
            return
        texto = re.sub(r"\s+", " ", "".join(p["texto"] for p in atual)).strip()
        if texto:
            blocos.append(Bloco(inicio=atual[0]["inicio"], fim=atual[-1]["fim"], texto=texto))
        atual.clear()

    for p in palavras:
        if atual:
            anterior = atual[-1]["texto"].strip()
            decorrido = p["fim"] - atual[0]["inicio"]
            corta = (
                p["inicio"] - atual[-1]["fim"] > pausa_maxima
                or (decorrido > duracao_maxima and anterior.endswith((".", "?", "!", "…")))
                or (decorrido > duracao_maxima * 1.5 and anterior.endswith((",", ";", ":")))
                or decorrido > duracao_limite
            )
            if corta:
                fecha()
        atual.append(p)
    fecha()
    return blocos


# --------------------------------------------------------------------------- #
# 4) Exportações
# --------------------------------------------------------------------------- #
def gerar_docx(res: Resultado) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    doc = Document()
    estilo = doc.styles["Normal"]
    estilo.font.name = "Calibri"
    estilo.font.size = Pt(11)

    meta = res.meta
    doc.add_heading(meta.get("titulo") or "Decupagem", level=0)

    ficha = [
        ("Canal", meta.get("canal", "")),
        ("Publicado em", meta.get("publicado_em", "")),
        ("Duração", hms(meta.get("duracao", 0.0)) if meta.get("duracao") else ""),
        ("Link", meta.get("url", "")),
        ("Trechos", str(len(res.blocos))),
        ("Decupado em", dt.datetime.now().strftime("%d/%m/%Y %H:%M")),
    ]
    tabela = doc.add_table(rows=0, cols=2)
    tabela.style = "Light Grid Accent 1"
    for rotulo, valor in ficha:
        if not valor:
            continue
        celulas = tabela.add_row().cells
        celulas[0].paragraphs[0].add_run(rotulo).bold = True
        celulas[1].text = str(valor)
    doc.add_paragraph()

    doc.add_heading("Transcrição", level=1)
    for b in res.blocos:
        marcador = doc.add_paragraph()
        marcador.paragraph_format.space_before = Pt(10)
        marcador.paragraph_format.space_after = Pt(2)
        run = marcador.add_run(f"[{hms(b.inicio)} – {hms(b.fim)}]")
        run.bold = True
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x44, 0x44, 0x44)

        corpo = doc.add_paragraph(b.texto)
        corpo.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        corpo.paragraph_format.space_after = Pt(6)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def gerar_txt(res: Resultado) -> bytes:
    linhas = [res.meta.get("titulo", "Decupagem"), res.meta.get("url", ""), ""]
    for b in res.blocos:
        linhas.append(f"[{hms(b.inicio)} – {hms(b.fim)}] {b.texto}")
        linhas.append("")
    return "\n".join(linhas).encode("utf-8")


def gerar_srt(res: Resultado) -> bytes:
    partes = [
        f"{i}\n{hms(b.inicio, com_ms=True)} --> {hms(b.fim, com_ms=True)}\n{b.texto}\n"
        for i, b in enumerate(res.blocos, start=1)
    ]
    return "\n".join(partes).encode("utf-8")


# --------------------------------------------------------------------------- #
# 5) Orquestração
# --------------------------------------------------------------------------- #
def processar(cfg: dict, status) -> Resultado:
    log = status.write
    pasta = WORKDIR / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    pasta.mkdir(parents=True, exist_ok=True)

    # --- áudio ---
    status.update(label="Baixando o vídeo do YouTube…")
    cookies = cfg["cookies_file"]
    if cfg["cookies_bytes"]:
        arquivo_cookies = pasta / "cookies.txt"
        arquivo_cookies.write_bytes(cfg["cookies_bytes"])
        cookies = str(arquivo_cookies)
    origem, meta = baixar_audio(
        cfg["url"],
        pasta,
        cookies_browser=cfg["cookies_browser"],
        cookies_file=cookies,
        solver_remoto=cfg["solver_remoto"],
        ca_bundle=cfg["ca_bundle"],
        ignorar_certificado=cfg["ignorar_certificado"],
        log=log,
    )

    status.update(label="Extraindo o áudio do vídeo…")
    wav = converter_para_wav16k(origem, pasta / "audio16k.wav")
    log(f"Áudio pronto: {wav.name} ({wav.stat().st_size / 1e6:.1f} MB)")

    # O vídeo (baixado ou enviado) só serve de ponte para o áudio; uma vez
    # extraído, o arquivo temporário do vídeo é descartado.
    if origem != wav and origem.exists():
        origem.unlink(missing_ok=True)
        log(f"Vídeo temporário removido: {origem.name}")

    # --- transcrição ---
    status.update(label="Transcrevendo o áudio…")
    barra = st.progress(0.0, text="Transcrição em andamento…")
    palavras, idioma = transcrever_local(
        wav,
        progresso=lambda p: barra.progress(p, text=f"Transcrição: {p:.0%}"),
        log=log,
    )
    barra.empty()
    blocos = montar_blocos(palavras)

    meta["duracao"] = meta.get("duracao") or (blocos[-1].fim if blocos else 0.0)
    log(f"{len(blocos)} trechos de fala gerados.")
    return Resultado(blocos=blocos, meta=meta, idioma=idioma)


# --------------------------------------------------------------------------- #
# 6) Interface
# --------------------------------------------------------------------------- #
st.set_page_config(page_title=APP_TITLE, page_icon="🎬", layout="centered")
st.title("🎬 " + APP_TITLE)
st.caption(
    "Cole o link de um vídeo do YouTube. O áudio é extraído, transcrito com "
    "marcação de tempo e entregue em .docx."
)

# Em servidor o YouTube recusa o download.
YOUTUBE_BLOQUEADO = NA_NUVEM and not proxy_configurado()

if YOUTUBE_BLOQUEADO:
    st.warning(
        "O YouTube recusa downloads vindos de servidores, e este app está hospedado "
        "em um. O link costuma falhar aqui. (Para habilitar o link, defina "
        "`YTDLP_PROXY` nos secrets.)"
    )

url = st.text_input("Link do vídeo", placeholder="https://www.youtube.com/watch?v=…")

executar = st.button("▶️ Decupar", type="primary", use_container_width=True)

if executar:
    erros = []
    if not valida_url(url):
        erros.append("Informe um link válido (começando com http:// ou https://).")

    if erros:
        for e in erros:
            st.error(e)
    else:
        cfg = {
            "url": url.strip(),
            # O YouTube às vezes exige cookies de uma sessão logada; sem interface
            # de configuração, o app roda sem eles.
            "cookies_browser": None,
            "cookies_file": None,
            "cookies_bytes": None,
            # Em servidor o solucionador de desafios é praticamente obrigatório.
            "solver_remoto": NA_NUVEM,
            "ca_bundle": None,
            "ignorar_certificado": False,
        }
        try:
            with st.status("Preparando…", expanded=True) as status:
                inicio = time.time()
                resultado = processar(cfg, status)
                status.update(
                    label=f"Concluído em {hms(time.time() - inicio)}",
                    state="complete",
                    expanded=False,
                )
            st.session_state["resultado"] = resultado
        except Exception as exc:
            st.error(f"Não foi possível concluir a decupagem: {exc}")
            texto_erro = str(exc).lower()
            if "certificate" in texto_erro or "ssl" in texto_erro:
                st.warning(
                    "Erro de certificado TLS — comum em rede corporativa com proxy que "
                    "inspeciona o tráfego. Se estiver em VPN corporativa, tente fora dela."
                )
            elif any(m in texto_erro for m in ("403", "forbidden", "bot", "fragment",
                                                "não entregou o conteúdo")):
                if NA_NUVEM:
                    st.warning(
                        "**O YouTube bloqueou o download.** Não é falha do app: o "
                        "YouTube recusa conexões vindas de IPs de datacenter, que é o "
                        "caso de qualquer servidor — inclusive o do Streamlit Cloud.\n\n"
                        "Para que o link do YouTube funcione aqui, só saindo por um "
                        "proxy residencial: basta definir `YTDLP_PROXY` nos *secrets* "
                        "do app que ele passa a ser usado automaticamente."
                    )
                else:
                    st.warning(
                        "**O YouTube recusou o download.** Costuma ser temporário; "
                        "tente de novo em alguns minutos."
                    )
            with st.expander("Detalhes técnicos"):
                import traceback

                st.code(traceback.format_exc())


# --------------------------------------------------------------------------- #
# Resultado
# --------------------------------------------------------------------------- #
res: Resultado | None = st.session_state.get("resultado")
if res:
    st.success(f"{len(res.blocos)} trechos transcritos.")

    nome_base = slug(res.meta.get("titulo", "decupagem"))
    c1, c2, c3 = st.columns([2, 1, 1])
    c1.download_button(
        "📄 Baixar .docx",
        data=gerar_docx(res),
        file_name=f"{nome_base}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        type="primary",
        use_container_width=True,
    )
    c2.download_button(
        "TXT", gerar_txt(res),
        file_name=f"{nome_base}.txt", mime="text/plain", use_container_width=True,
    )
    c3.download_button(
        "SRT", gerar_srt(res),
        file_name=f"{nome_base}.srt", mime="text/plain", use_container_width=True,
    )

    st.dataframe(
        [{"Início": hms(b.inicio), "Fim": hms(b.fim), "Fala": b.texto} for b in res.blocos],
        use_container_width=True,
        hide_index=True,
        height=520,
    )

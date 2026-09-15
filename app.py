# -*- coding: utf-8 -*-
"""
Decupagem de vídeos do YouTube
==============================
Interface Streamlit que recebe o link de um vídeo do YouTube, extrai o áudio,
transcreve com marcação de tempo, identifica quem falou cada trecho
(diarização) e exporta tudo para um arquivo .docx.

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
    """Um trecho contíguo de fala de um único participante."""

    inicio: float
    fim: float
    texto: str
    falante: str | None = None


@dataclass
class Resultado:
    blocos: list[Bloco]
    meta: dict = field(default_factory=dict)
    idioma: str | None = None

    @property
    def falantes(self) -> list[str]:
        vistos: list[str] = []
        for b in self.blocos:
            if b.falante and b.falante not in vistos:
                vistos.append(b.falante)
        return vistos


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


def _extrair_com_rodizio(opts: dict, url: str, log: Callable[[str], None]) -> dict:
    """Baixa tentando clientes diferentes do YouTube até um funcionar.

    Cada "player client" do YouTube entrega as URLs de mídia sob regras próprias:
    uns exigem que o desafio de JavaScript seja resolvido, outros não. Quando um
    é recusado com 403 ou não devolve formato nenhum, outro costuma passar.
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
                return ydl.extract_info(url, download=True)
        except Exception as exc:
            ultimo_erro = exc
            if any(marca in str(exc).lower() for marca in ERROS_DEFINITIVOS):
                raise
            log("Não deu certo; tentando outro cliente do YouTube…")

    raise ultimo_erro  # type: ignore[misc]


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

    # Baixa a trilha de áudio crua (m4a/webm/opus) e deixa a conversão para o
    # converter_para_wav16k: o binário do imageio-ffmpeg não se chama "ffmpeg.exe",
    # e o yt-dlp só aceita esse nome ao procurar o executável.
    opts: dict = {
        "format": "bestaudio/best",
        "outtmpl": str(destino / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "logger": _LoggerYtdlp(log),
    }
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

    info = _extrair_com_rodizio(opts, url, log)

    vid = info.get("id", "audio")
    # As tentativas anteriores podem ter deixado downloads pela metade (.part).
    candidatos = sorted(
        (p for p in destino.glob(f"{vid}.*")
         if p.is_file() and p.suffix.lower() not in (".part", ".ytdl", ".temp")),
        key=lambda p: -p.stat().st_size,
    )
    if not candidatos:
        raise RuntimeError("O download terminou, mas nenhum arquivo de áudio foi encontrado.")

    meta = {
        "titulo": info.get("title") or "(sem título)",
        "canal": info.get("uploader") or info.get("channel") or "",
        "duracao": float(info.get("duration") or 0.0),
        "url": info.get("webpage_url") or url,
        "publicado_em": _data_legivel(info.get("upload_date")),
        "id": vid,
    }
    log(f"Áudio obtido: {candidatos[0].name}")
    return candidatos[0], meta


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
@st.cache_resource(show_spinner=False)
def carregar_whisper(modelo: str, dispositivo: str, compute_type: str):
    from faster_whisper import WhisperModel

    return WhisperModel(modelo, device=dispositivo, compute_type=compute_type)


def transcrever_local(
    wav: Path,
    modelo: str,
    idioma: str | None,
    dispositivo: str,
    progresso: Callable[[float], None] = lambda _p: None,
    log: Callable[[str], None] = lambda _m: None,
) -> tuple[list[dict], str]:
    """Transcreve e devolve (lista de palavras com tempo, idioma detectado)."""
    compute_type = "float16" if dispositivo == "cuda" else "int8"
    log(f"Carregando o modelo Whisper '{modelo}' ({dispositivo}/{compute_type})…")
    modelo_w = carregar_whisper(modelo, dispositivo, compute_type)

    segmentos, info = modelo_w.transcribe(
        str(wav),
        language=idioma,
        task="transcribe",
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    total = float(getattr(info, "duration", 0.0)) or 1.0
    log(f"Idioma: {info.language} (confiança {info.language_probability:.0%})")

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
    return palavras, info.language


# --------------------------------------------------------------------------- #
# 3) Diarização — pyannote.audio (quem falou o quê)
# --------------------------------------------------------------------------- #
def _modelos_de_diarizacao() -> list[str]:
    """Checkpoints a tentar, do mais adequado à versão instalada para o mais antigo."""
    try:
        import pyannote.audio

        if int(pyannote.audio.__version__.split(".")[0]) >= 4:
            return ["pyannote/speaker-diarization-community-1",
                    "pyannote/speaker-diarization-3.1"]
    except Exception:
        pass
    return ["pyannote/speaker-diarization-3.1"]


@st.cache_resource(show_spinner=False)
def carregar_pyannote(token: str, dispositivo: str):
    import torch
    from pyannote.audio import Pipeline

    falhas: list[str] = []
    for checkpoint in _modelos_de_diarizacao():
        for nome_do_parametro in ("token", "use_auth_token"):  # 4.x e 3.x
            try:
                pipe = Pipeline.from_pretrained(checkpoint, **{nome_do_parametro: token})
            except TypeError:
                continue  # o parâmetro não existe nesta versão
            except Exception as exc:
                falhas.append(f"{checkpoint}: {exc}")
                break
            if pipe is not None:
                return pipe.to(torch.device(dispositivo))
            falhas.append(f"{checkpoint}: acesso negado ao modelo")
            break

    detalhe = "; ".join(falhas) or "nenhum modelo pôde ser carregado"
    raise RuntimeError(
        "Não foi possível carregar o pyannote. Confira se o token do Hugging Face está "
        "correto e se você aceitou os termos do modelo de diarização e do "
        "'pyannote/segmentation-3.0' no site do Hugging Face. Detalhe: " + detalhe
    )


def carregar_wav_em_memoria(wav: Path):
    """Lê o WAV 16 kHz mono direto para um tensor.

    Entregar o áudio já carregado evita que o pyannote tente decodificar o arquivo
    por conta própria — no Windows isso depende das bibliotecas do FFmpeg, que o
    ffmpeg empacotado não fornece.
    """
    import wave

    import numpy as np
    import torch

    with wave.open(str(wav), "rb") as f:
        canais, largura, taxa = f.getnchannels(), f.getsampwidth(), f.getframerate()
        dados = f.readframes(f.getnframes())

    if largura != 2:
        raise RuntimeError(f"Esperado áudio PCM de 16 bits, veio de {largura * 8} bits.")

    amostras = np.frombuffer(dados, dtype="<i2").astype("float32") / 32768.0
    if canais > 1:
        amostras = amostras.reshape(-1, canais).mean(axis=1)
    return {"waveform": torch.from_numpy(amostras.copy()).unsqueeze(0), "sample_rate": taxa}


def diarizar(
    wav: Path,
    token: str,
    dispositivo: str,
    num_falantes: int | None,
    min_falantes: int | None,
    max_falantes: int | None,
    log: Callable[[str], None] = lambda _m: None,
) -> list[tuple[float, float, str]]:
    log("Carregando o modelo de diarização (a primeira vez baixa alguns MB)…")
    pipe = carregar_pyannote(token, dispositivo)

    kwargs: dict = {}
    if num_falantes:
        kwargs["num_speakers"] = int(num_falantes)
    else:
        if min_falantes:
            kwargs["min_speakers"] = int(min_falantes)
        if max_falantes:
            kwargs["max_speakers"] = int(max_falantes)

    resultado = pipe(carregar_wav_em_memoria(wav), **kwargs)
    # O pyannote 4 pode devolver um objeto com a anotação dentro; o 3.x devolve a
    # anotação diretamente.
    anotacao = getattr(resultado, "speaker_diarization", resultado)
    turnos = [
        (float(t.start), float(t.end), str(rotulo))
        for t, _, rotulo in anotacao.itertracks(yield_label=True)
    ]
    turnos.sort(key=lambda x: x[0])
    log(f"{len({t[2] for t in turnos})} falante(s) em {len(turnos)} turnos de fala.")
    return turnos


# --------------------------------------------------------------------------- #
# 4) Casamento palavra <-> falante e montagem dos blocos
# --------------------------------------------------------------------------- #
def falante_da_palavra(
    inicio: float, fim: float, turnos: list[tuple[float, float, str]]
) -> str | None:
    """Escolhe o turno com maior sobreposição; sem sobreposição, o mais próximo."""
    melhor, melhor_sobrep = None, 0.0
    for t_ini, t_fim, rotulo in turnos:
        sobrep = min(fim, t_fim) - max(inicio, t_ini)
        if sobrep > melhor_sobrep:
            melhor, melhor_sobrep = rotulo, sobrep
    if melhor:
        return melhor

    centro = (inicio + fim) / 2
    proximo, menor_dist = None, float("inf")
    for t_ini, t_fim, rotulo in turnos:
        dist = 0.0 if t_ini <= centro <= t_fim else min(abs(centro - t_ini), abs(centro - t_fim))
        if dist < menor_dist:
            proximo, menor_dist = rotulo, dist
    return proximo


def montar_blocos(
    palavras: list[dict],
    turnos: list[tuple[float, float, str]] | None,
    pausa_maxima: float = 2.0,
    duracao_maxima: float = 40.0,
    duracao_limite: float = 75.0,
) -> list[Bloco]:
    """Agrupa palavras em blocos de fala.

    O corte acontece na troca de falante, em pausas longas e, quando o bloco já
    está comprido, na primeira fronteira natural do texto — ponto final primeiro,
    vírgula depois. Acima de `duracao_limite` o corte é forçado, porque fala
    corrida sem pontuação renderia parágrafos intransponíveis no documento.
    """
    if not palavras:
        return []

    for p in palavras:
        p["falante"] = falante_da_palavra(p["inicio"], p["fim"], turnos) if turnos else None

    blocos: list[Bloco] = []
    atual: list[dict] = []

    def fecha():
        if not atual:
            return
        texto = re.sub(r"\s+", " ", "".join(p["texto"] for p in atual)).strip()
        if texto:
            blocos.append(
                Bloco(
                    inicio=atual[0]["inicio"],
                    fim=atual[-1]["fim"],
                    texto=texto,
                    falante=atual[0]["falante"],
                )
            )
        atual.clear()

    for p in palavras:
        if atual:
            anterior = atual[-1]["texto"].strip()
            decorrido = p["fim"] - atual[0]["inicio"]
            corta = (
                p["falante"] != atual[-1]["falante"]
                or p["inicio"] - atual[-1]["fim"] > pausa_maxima
                or (decorrido > duracao_maxima and anterior.endswith((".", "?", "!", "…")))
                or (decorrido > duracao_maxima * 1.5 and anterior.endswith((",", ";", ":")))
                or decorrido > duracao_limite
            )
            if corta:
                fecha()
        atual.append(p)
    fecha()
    return blocos


def renomear_falantes(blocos: list[Bloco], prefixo: str = "FALANTE") -> list[Bloco]:
    """Troca SPEAKER_00/01… por rótulos legíveis, na ordem de entrada em cena."""
    mapa: dict[str, str] = {}
    for b in blocos:
        if b.falante and b.falante not in mapa:
            mapa[b.falante] = f"{prefixo} {len(mapa) + 1}"
    for b in blocos:
        if b.falante:
            b.falante = mapa[b.falante]
    return blocos


# --------------------------------------------------------------------------- #
# 5) Backend alternativo — AssemblyAI (transcrição + diarização na nuvem)
# --------------------------------------------------------------------------- #
def transcrever_assemblyai(
    audio: Path,
    api_key: str,
    idioma: str | None,
    falantes_esperados: int | None,
    log: Callable[[str], None] = lambda _m: None,
) -> tuple[list[Bloco], str]:
    import requests

    base = "https://api.assemblyai.com/v2"
    headers = {"authorization": api_key}

    log("Enviando o áudio para a AssemblyAI…")
    with open(audio, "rb") as fh:
        envio = requests.post(f"{base}/upload", headers=headers, data=fh, timeout=900)
    envio.raise_for_status()
    audio_url = envio.json()["upload_url"]

    corpo: dict = {"audio_url": audio_url, "speaker_labels": True, "punctuate": True}
    if idioma:
        corpo["language_code"] = idioma
    else:
        corpo["language_detection"] = True
    if falantes_esperados:
        corpo["speakers_expected"] = int(falantes_esperados)

    criacao = requests.post(f"{base}/transcript", headers=headers, json=corpo, timeout=60)
    criacao.raise_for_status()
    tid = criacao.json()["id"]

    log("Processando na nuvem…")
    while True:
        r = requests.get(f"{base}/transcript/{tid}", headers=headers, timeout=60)
        r.raise_for_status()
        dados = r.json()
        if dados["status"] == "completed":
            break
        if dados["status"] == "error":
            raise RuntimeError(f"AssemblyAI: {dados.get('error')}")
        time.sleep(3)

    blocos = [
        Bloco(
            inicio=u["start"] / 1000.0,
            fim=u["end"] / 1000.0,
            texto=u["text"].strip(),
            falante=f"FALANTE {u['speaker']}",
        )
        for u in (dados.get("utterances") or [])
        if u.get("text", "").strip()
    ]
    if not blocos and dados.get("text"):
        blocos = [Bloco(0.0, float(dados.get("audio_duration") or 0.0), dados["text"], None)]
    return blocos, dados.get("language_code") or (idioma or "")


# --------------------------------------------------------------------------- #
# 6) Exportações
# --------------------------------------------------------------------------- #
def gerar_docx(
    res: Resultado,
    apelidos: dict[str, str],
    com_tempos: bool = True,
    com_capa: bool = True,
    formato: str = "Parágrafos",
) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    doc = Document()
    estilo = doc.styles["Normal"]
    estilo.font.name = "Calibri"
    estilo.font.size = Pt(11)

    meta = res.meta
    doc.add_heading(meta.get("titulo") or "Decupagem", level=0)

    if com_capa:
        n_falantes = len({b.falante for b in res.blocos if b.falante})
        linhas = [
            ("Canal", meta.get("canal", "")),
            ("Publicado em", meta.get("publicado_em", "")),
            ("Duração", hms(meta.get("duracao", 0.0)) if meta.get("duracao") else ""),
            ("Link", meta.get("url", "")),
            ("Idioma", (res.idioma or "").upper()),
            ("Falantes identificados", str(n_falantes) if n_falantes else ""),
            ("Decupado em", dt.datetime.now().strftime("%d/%m/%Y %H:%M")),
        ]
        tabela = doc.add_table(rows=0, cols=2)
        tabela.style = "Light Grid Accent 1"
        for rotulo, valor in linhas:
            if not valor:
                continue
            celulas = tabela.add_row().cells
            celulas[0].paragraphs[0].add_run(rotulo).bold = True
            celulas[1].text = str(valor)
        doc.add_paragraph()

    doc.add_heading("Transcrição", level=1)

    if formato == "Tabela":
        tabela = doc.add_table(rows=1, cols=3)
        tabela.style = "Light List Accent 1"
        cabecalho = tabela.rows[0].cells
        for i, titulo in enumerate(["Tempo", "Falante", "Fala"]):
            cabecalho[i].paragraphs[0].add_run(titulo).bold = True
        for b in res.blocos:
            linha = tabela.add_row().cells
            linha[0].text = f"{hms(b.inicio)}\n{hms(b.fim)}" if com_tempos else hms(b.inicio)
            linha[1].text = apelidos.get(b.falante or "", b.falante or "")
            linha[2].text = b.texto
    else:
        for b in res.blocos:
            partes = []
            if com_tempos:
                partes.append(f"[{hms(b.inicio)} – {hms(b.fim)}]")
            nome = apelidos.get(b.falante or "", b.falante or "")
            if nome:
                partes.append(nome)
            if partes:
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(10)
                p.paragraph_format.space_after = Pt(2)
                run = p.add_run("  ".join(partes))
                run.bold = True
                run.font.size = Pt(9)
                run.font.color.rgb = RGBColor(0x44, 0x44, 0x44)
            corpo = doc.add_paragraph(b.texto)
            corpo.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            corpo.paragraph_format.space_after = Pt(6)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def gerar_txt(res: Resultado, apelidos: dict[str, str], com_tempos: bool = True) -> bytes:
    linhas = [res.meta.get("titulo", "Decupagem"), res.meta.get("url", ""), ""]
    for b in res.blocos:
        prefixo = []
        if com_tempos:
            prefixo.append(f"[{hms(b.inicio)} – {hms(b.fim)}]")
        nome = apelidos.get(b.falante or "", b.falante or "")
        if nome:
            prefixo.append(f"{nome}:")
        linhas.append((" ".join(prefixo) + " " + b.texto).strip())
        linhas.append("")
    return "\n".join(linhas).encode("utf-8")


def gerar_srt(res: Resultado, apelidos: dict[str, str]) -> bytes:
    partes = []
    for i, b in enumerate(res.blocos, start=1):
        nome = apelidos.get(b.falante or "", b.falante or "")
        texto = f"{nome}: {b.texto}" if nome else b.texto
        partes.append(
            f"{i}\n{hms(b.inicio, com_ms=True)} --> {hms(b.fim, com_ms=True)}\n{texto}\n"
        )
    return "\n".join(partes).encode("utf-8")


# --------------------------------------------------------------------------- #
# 7) Orquestração
# --------------------------------------------------------------------------- #
def processar(cfg: dict, status) -> Resultado:
    log = status.write
    pasta = WORKDIR / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    pasta.mkdir(parents=True, exist_ok=True)

    # --- áudio ---
    if cfg["fonte"] == "arquivo":
        origem = pasta / cfg["arquivo_nome"]
        origem.write_bytes(cfg["arquivo_bytes"])
        meta = {"titulo": Path(cfg["arquivo_nome"]).stem, "url": "", "canal": "",
                "publicado_em": "", "duracao": 0.0}
        log(f"Arquivo recebido: {origem.name}")
    else:
        status.update(label="Baixando o áudio do YouTube…")
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

    status.update(label="Convertendo o áudio para 16 kHz mono…")
    wav = converter_para_wav16k(origem, pasta / "audio16k.wav")
    log(f"Áudio pronto: {wav.name} ({wav.stat().st_size / 1e6:.1f} MB)")

    idioma = None if cfg["idioma"] == "auto" else cfg["idioma"]

    # --- transcrição (+ diarização) ---
    if cfg["backend"] == "AssemblyAI (nuvem)":
        status.update(label="Transcrevendo e separando os falantes (AssemblyAI)…")
        blocos, idioma_detectado = transcrever_assemblyai(
            wav, cfg["assembly_key"], idioma, cfg["num_falantes"], log
        )
    else:
        status.update(label="Transcrevendo o áudio…")
        barra = st.progress(0.0, text="Transcrição em andamento…")
        palavras, idioma_detectado = transcrever_local(
            wav,
            cfg["modelo"],
            idioma,
            cfg["dispositivo"],
            progresso=lambda p: barra.progress(p, text=f"Transcrição: {p:.0%}"),
            log=log,
        )
        barra.empty()

        turnos = None
        if cfg["diarizar"]:
            status.update(label="Identificando quem fala cada trecho…")
            turnos = diarizar(
                wav,
                cfg["hf_token"],
                cfg["dispositivo"],
                cfg["num_falantes"],
                cfg["min_falantes"],
                cfg["max_falantes"],
                log,
            )
        blocos = renomear_falantes(montar_blocos(palavras, turnos))

    meta["duracao"] = meta.get("duracao") or (blocos[-1].fim if blocos else 0.0)
    log(f"{len(blocos)} trechos de fala gerados.")
    return Resultado(blocos=blocos, meta=meta, idioma=idioma_detectado)


# --------------------------------------------------------------------------- #
# 8) Interface
# --------------------------------------------------------------------------- #
st.set_page_config(page_title=APP_TITLE, page_icon="🎬", layout="wide")
st.title("🎬 " + APP_TITLE)
st.caption(
    "Baixa o áudio de um vídeo do YouTube, transcreve com marcação de tempo, "
    "separa quem falou cada trecho e exporta em .docx."
)

with st.sidebar:
    st.header("⚙️ Configuração")

    backend = st.radio(
        "Motor de transcrição",
        ["Local (faster-whisper)", "AssemblyAI (nuvem)"],
        help="O modo local roda na sua máquina e é gratuito. O modo AssemblyAI é mais "
             "rápido e já traz a separação de falantes pronta, mas exige chave de API.",
    )

    if NA_NUVEM and backend == "Local (faster-whisper)":
        st.caption(
            "O servidor tem pouca memória: use o modelo `small` ou menor, ou troque "
            "para a AssemblyAI. A diarização com pyannote costuma estourar o limite."
        )

    assembly_key = hf_token = ""
    modelo = "small"
    dispositivo = "cpu"
    diarizar_on = False

    if backend == "Local (faster-whisper)":
        modelo = st.selectbox(
            "Modelo Whisper",
            ["tiny", "base", "small", "medium", "large-v3"],
            index=2,
            help="Modelos maiores transcrevem melhor e demoram mais. Em CPU, "
                 "'small' costuma ser o melhor equilíbrio.",
        )
        try:
            import torch

            tem_gpu = torch.cuda.is_available()
        except Exception:
            tem_gpu = False
        dispositivo = st.selectbox(
            "Processamento", ["cuda", "cpu"] if tem_gpu else ["cpu"], index=0
        )

        diarizar_on = st.toggle(
            "Identificar os falantes (diarização)",
            value=False,
            help="Usa o pyannote.audio. Exige o pyannote.audio instalado, "
                 "um token do Hugging Face e a aceitação dos termos dos modelos "
                 "pyannote/speaker-diarization-3.1 e pyannote/segmentation-3.0.",
        )
        if diarizar_on:
            hf_token = st.text_input(
                "Token do Hugging Face",
                type="password",
                value=os.environ.get("HF_TOKEN", ""),
                help="Crie em huggingface.co/settings/tokens (permissão de leitura).",
            )
    else:
        assembly_key = st.text_input(
            "Chave da AssemblyAI",
            type="password",
            value=os.environ.get("ASSEMBLYAI_API_KEY", ""),
            help="Obtida em assemblyai.com. A separação de falantes já vem incluída.",
        )

    NOMES_IDIOMA = {
        "pt": "Português", "auto": "Detectar automaticamente", "en": "Inglês",
        "es": "Espanhol", "fr": "Francês", "it": "Italiano", "de": "Alemão",
    }
    idioma = st.selectbox(
        "Idioma do áudio",
        list(NOMES_IDIOMA),
        index=0,
        format_func=NOMES_IDIOMA.get,
    )

    st.divider()
    st.subheader("Falantes")
    sabe_quantos = st.toggle("Sei quantas pessoas falam no vídeo", value=False)
    num_falantes = min_falantes = max_falantes = None
    if sabe_quantos:
        num_falantes = st.number_input("Quantidade de falantes", 1, 20, 2)
    elif backend == "Local (faster-whisper)":
        min_falantes, max_falantes = st.slider("Faixa provável", 1, 12, (1, 6))

    st.divider()
    with st.expander("Vídeo restrito ou bloqueado"):
        st.caption(
            "O YouTube às vezes exige login ou verificação. Nesses casos, reaproveite "
            "os cookies de um navegador em que você já esteja logado."
        )
        opcoes_cookies = ["Nenhum", "Enviar arquivo cookies.txt"]
        if not NA_NUVEM:
            opcoes_cookies.insert(1, "Do navegador")
        usar_cookies = st.selectbox("Cookies", opcoes_cookies)

        cookies_browser = cookies_file = cookies_bytes = None
        if usar_cookies == "Do navegador":
            cookies_browser = st.selectbox(
                "Navegador", ["chrome", "edge", "firefox", "brave", "opera", "vivaldi"]
            )
        elif usar_cookies == "Enviar arquivo cookies.txt":
            enviado = st.file_uploader("cookies.txt (formato Netscape)", type=["txt"])
            if enviado is not None:
                cookies_bytes = enviado.getvalue()
            st.caption(
                "Exporte com uma extensão de navegador como a 'Get cookies.txt LOCALLY', "
                "estando logado no YouTube."
            )

        solver_remoto = st.checkbox(
            "Baixar o solucionador de desafios do YouTube",
            value=NA_NUVEM,
            help="O yt-dlp busca no GitHub o script oficial que resolve os desafios do "
                 "YouTube. Praticamente obrigatório em servidor; local, use só se o "
                 "download falhar por falta de formatos.",
        )

    with st.expander("Erro de certificado (rede corporativa)"):
        if TLS["certificados_windows"]:
            st.caption(
                f"Em uso: {TLS['certificados_windows']} certificados do Windows somados "
                f"aos públicos do certifi"
                + (", mais validação dinâmica pelo truststore." if TLS["truststore"] else ".")
                + " Isso costuma resolver o CERTIFICATE_VERIFY_FAILED em rede com proxy "
                "corporativo; as opções abaixo são para quando o erro persiste."
            )
        else:
            st.caption(
                "Não foi possível ler os certificados do Windows. Se aparecer "
                "CERTIFICATE_VERIFY_FAILED, aponte abaixo o .pem da sua empresa."
            )
        st.caption(f"Python em uso: `{sys.executable}`")
        ca_bundle = st.text_input(
            "Arquivo .pem com a autoridade certificadora", placeholder="opcional"
        ) or None
        ignorar_certificado = st.checkbox(
            "Ignorar a verificação do certificado (inseguro)",
            value=False,
            help="Último recurso: a conexão deixa de ser verificada e fica sujeita a "
                 "interceptação. Use apenas em rede de confiança.",
        )

fonte = st.radio("Fonte do áudio", ["Link do YouTube", "Arquivo local"], horizontal=True)

url = ""
upload = None
if fonte == "Link do YouTube":
    url = st.text_input("Link do vídeo", placeholder="https://www.youtube.com/watch?v=…")
else:
    upload = st.file_uploader(
        "Áudio ou vídeo",
        type=["mp3", "wav", "m4a", "ogg", "opus", "flac", "mp4", "mkv", "webm", "mov"],
    )

executar = st.button("▶️ Decupar", type="primary", use_container_width=True)

if executar:
    erros = []
    if fonte == "Link do YouTube" and not valida_url(url):
        erros.append("Informe um link válido (começando com http:// ou https://).")
    if fonte == "Arquivo local" and upload is None:
        erros.append("Envie um arquivo de áudio ou vídeo.")
    if backend == "AssemblyAI (nuvem)" and not assembly_key:
        erros.append("Informe a chave da API da AssemblyAI.")
    if backend == "Local (faster-whisper)" and diarizar_on and not hf_token:
        erros.append("Informe o token do Hugging Face para usar a diarização.")

    if erros:
        for e in erros:
            st.error(e)
    else:
        cfg = {
            "fonte": "arquivo" if fonte == "Arquivo local" else "youtube",
            "url": url.strip(),
            "arquivo_bytes": upload.getvalue() if upload else None,
            "arquivo_nome": upload.name if upload else None,
            "backend": backend,
            "modelo": modelo,
            "dispositivo": dispositivo,
            "idioma": idioma,
            "diarizar": diarizar_on,
            "hf_token": hf_token,
            "assembly_key": assembly_key,
            "num_falantes": num_falantes,
            "min_falantes": min_falantes,
            "max_falantes": max_falantes,
            "cookies_browser": cookies_browser,
            "cookies_file": cookies_file,
            "cookies_bytes": cookies_bytes,
            "solver_remoto": solver_remoto,
            "ca_bundle": ca_bundle,
            "ignorar_certificado": ignorar_certificado,
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
            st.session_state["apelidos"] = {f: f for f in resultado.falantes}
        except Exception as exc:
            st.error(f"Não foi possível concluir a decupagem: {exc}")
            texto_erro = str(exc).lower()
            if "certificate" in texto_erro or "ssl" in texto_erro:
                st.warning(
                    "Erro de certificado TLS — comum em rede corporativa com proxy. "
                    "Abra **Erro de certificado (rede corporativa)** na barra lateral: "
                    "informe o .pem da sua empresa ou, em último caso, desative a "
                    "verificação. Se estiver em casa, tente sem o proxy da VPN."
                )
            elif "403" in texto_erro or "forbidden" in texto_erro:
                st.warning(
                    "**HTTP 403 no download da mídia.** Os dados do vídeo chegaram, mas "
                    "o YouTube recusou a URL do áudio."
                    + (
                        "\n\nEm servidor isso é a regra, não a exceção: o YouTube trata "
                        "IPs de datacenter com desconfiança e exige que os desafios de "
                        "JavaScript sejam resolvidos. Verifique se o `packages.txt` do "
                        "repositório tem a linha `nodejs`, deixe marcada a opção de "
                        "baixar o solucionador de desafios e, se ainda assim falhar, "
                        "envie um `cookies.txt` — tudo na barra lateral.\n\n"
                        "Quando nada disso resolve, o caminho confiável é usar a fonte "
                        "**Arquivo local**: baixe o vídeo na sua máquina e envie aqui."
                        if NA_NUVEM else
                        "\n\nMarque *Baixar o solucionador de desafios do YouTube* na "
                        "barra lateral e tente de novo; se persistir, envie um "
                        "`cookies.txt` de uma sessão logada."
                    )
                )
            elif "sign in" in texto_erro or "bot" in texto_erro or "private" in texto_erro:
                st.warning(
                    "O YouTube pediu autenticação. Em **Vídeo restrito ou bloqueado** "
                    "na barra lateral, "
                    + ("envie um `cookies.txt` de uma sessão logada." if NA_NUVEM else
                       "escolha os cookies do navegador em que você já está logado.")
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
    col_esq, col_dir = st.columns([2, 1], gap="large")

    with col_dir:
        st.subheader("Nomes dos falantes")
        apelidos = st.session_state.get("apelidos", {})
        if res.falantes:
            for f in res.falantes:
                apelidos[f] = st.text_input(f, value=apelidos.get(f, f), key=f"nome_{f}")
            st.session_state["apelidos"] = apelidos
        else:
            st.info("Sem diarização: a transcrição sai apenas com os tempos.")

        st.subheader("Formato do documento")
        com_tempos = st.toggle("Incluir marcação de tempo", value=True)
        com_capa = st.toggle("Incluir ficha do vídeo", value=True)
        layout = st.radio("Layout", ["Parágrafos", "Tabela"], horizontal=True)

        nome_base = slug(res.meta.get("titulo", "decupagem"))
        st.download_button(
            "📄 Baixar .docx",
            data=gerar_docx(res, apelidos, com_tempos, com_capa, layout),
            file_name=f"{nome_base}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            type="primary",
            use_container_width=True,
        )
        c1, c2 = st.columns(2)
        c1.download_button(
            "TXT", gerar_txt(res, apelidos, com_tempos),
            file_name=f"{nome_base}.txt", mime="text/plain", use_container_width=True,
        )
        c2.download_button(
            "SRT", gerar_srt(res, apelidos),
            file_name=f"{nome_base}.srt", mime="text/plain", use_container_width=True,
        )

    with col_esq:
        st.subheader("Prévia da transcrição")
        busca = st.text_input("Filtrar por palavra", placeholder="opcional")
        visiveis = [b for b in res.blocos if not busca or busca.lower() in b.texto.lower()]
        st.caption(f"{len(visiveis)} de {len(res.blocos)} trechos")
        st.dataframe(
            [
                {
                    "Início": hms(b.inicio),
                    "Fim": hms(b.fim),
                    "Falante": apelidos.get(b.falante or "", b.falante or "—"),
                    "Fala": b.texto,
                }
                for b in visiveis
            ],
            use_container_width=True,
            hide_index=True,
            height=560,
        )

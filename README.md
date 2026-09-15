# Decupagem de vídeos do YouTube

Aplicativo Streamlit que recebe o link de um vídeo do YouTube, extrai o áudio,
transcreve com marcação de tempo, identifica quem falou cada trecho e exporta
o resultado em `.docx` (e também em `.txt` e `.srt`).

## Instalação

```bash
pip install -r requirements.txt
```

Isso já inclui o `ffmpeg` (via `imageio-ffmpeg`), então não é preciso instalar
nada fora do Python. O download passa de 2,5 GB por causa do PyTorch, que só é
usado pela identificação de falantes — se você não for usar esse recurso, pode
remover as três últimas linhas do `requirements.txt`.

Para identificar os falantes no modo local ainda é preciso:

1. criar um token de leitura em <https://huggingface.co/settings/tokens>;
2. aceitar os termos dos modelos de diarização no Hugging Face:
   [speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
   (usado pelo pyannote 4) ou
   [speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
   (pyannote 3), além do
   [segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0).
   O app tenta o modelo adequado à versão instalada e cai para o outro se
   precisar.

O token pode ser colado na barra lateral ou definido na variável de ambiente
`HF_TOKEN`.

## Uso

```bash
streamlit run app.py
```

O navegador abre em <http://localhost:8501>. Cole o link, clique em **Decupar** e,
ao final, baixe o `.docx`. Antes de exportar dá para renomear os falantes
(`FALANTE 1` → `Maria`, por exemplo), escolher entre layout em parágrafos ou em
tabela e decidir se os tempos entram no documento.

Também é possível enviar um arquivo de áudio ou vídeo do computador em vez de
usar um link.

## Os dois motores de transcrição

| | Local (faster-whisper) | AssemblyAI (nuvem) |
|---|---|---|
| Custo | gratuito | pago por hora de áudio |
| Privacidade | o áudio não sai da máquina | o áudio é enviado ao serviço |
| Velocidade | depende da CPU/GPU | rápida |
| Identificação de falantes | exige pyannote + token | já vem incluída |

No modo local, o modelo `small` costuma ser o melhor equilíbrio em CPU: cerca de
1 a 2 minutos de processamento por minuto de áudio. Com GPU NVIDIA, o
`large-v3` fica viável e a opção `cuda` aparece sozinha na barra lateral.

Para usar a AssemblyAI, cole a chave na barra lateral ou defina
`ASSEMBLYAI_API_KEY`.

## Como os falantes são atribuídos

O Whisper devolve o tempo de cada palavra e o pyannote devolve os intervalos de
cada participante. Cada palavra recebe o falante cujo intervalo tem maior
sobreposição com ela; em seguida as palavras viram blocos, que são cortados
quando o falante muda, quando há uma pausa maior que 2 segundos ou quando o
trecho passa de 45 segundos e termina em pontuação final.

Informar a quantidade de participantes na barra lateral melhora bastante o
resultado quando você já sabe quantas pessoas falam.

## Problemas comuns

**`CERTIFICATE_VERIFY_FAILED`** — rede corporativa com proxy que intercepta o
TLS. O app já valida os certificados pelo repositório do Windows (pacote
`truststore`), o que resolve a maioria dos casos. Se persistir, abra *Erro de
certificado (rede corporativa)* na barra lateral e informe o `.pem` da sua
empresa; a opção de ignorar a verificação existe como último recurso e deixa a
conexão sujeita a interceptação.

**`Sign in to confirm you're not a bot`** — o YouTube pediu autenticação. Em
*Vídeo restrito ou bloqueado*, escolha os cookies de um navegador em que você já
esteja logado.

**`No supported JavaScript runtime`** — o YouTube exige um interpretador de
JavaScript para liberar todos os formatos. O app usa automaticamente o Deno,
Node, Bun ou QuickJS que estiver instalado; se não houver nenhum, instale o
[Deno](https://deno.com/) ou o [Node.js](https://nodejs.org/). Quando ainda
assim faltarem formatos, marque *Baixar o solucionador de desafios do YouTube*.

**Transcrição muito lenta** — troque para um modelo menor (`base` ou `tiny`) ou
use o motor AssemblyAI.

## Arquivos

- `app.py` — o aplicativo inteiro (interface e processamento).
- `requirements.txt` — todas as dependências.

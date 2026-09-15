# Decupagem de vídeos do YouTube

Aplicativo Streamlit que recebe o link de um vídeo do YouTube, extrai o áudio,
transcreve com marcação de tempo e exporta em `.docx` (e também em `.txt` e
`.srt`).

Não há tela de configuração: o app transcreve **áudio em português**, com o
modelo **Whisper small**, rodando em **CPU**. Toda a transcrição acontece na
própria máquina — nada é enviado para serviços de terceiros.

## Instalação

```bash
pip install -r requirements.txt
```

Já vêm incluídos o `ffmpeg` (via `imageio-ffmpeg`) e o Node (via
`nodejs-wheel-binaries`), então não é preciso instalar nada fora do Python.

## Uso

```bash
streamlit run app.py
```

O navegador abre em <http://localhost:8501>. Cole o link, clique em **Decupar**
e baixe o `.docx` ao final. Também é possível enviar um arquivo de áudio ou
vídeo do computador em vez de usar um link.

Em CPU, a transcrição leva cerca de um terço da duração do áudio: um vídeo de
9 minutos fica pronto em aproximadamente 3 minutos.

## Como a transcrição é dividida

O Whisper devolve o tempo de cada palavra. As palavras viram blocos, cortados
quando há uma pausa maior que 2 segundos, ou — quando o bloco já passou de 40
segundos — na primeira fronteira natural do texto: ponto final primeiro, vírgula
depois. Acima de 75 segundos o corte é forçado, porque fala corrida sem
pontuação renderia parágrafos intransponíveis no documento.

## Problemas comuns

**`CERTIFICATE_VERIFY_FAILED`** — rede corporativa com proxy que intercepta o
TLS. O app já resolve isso sozinho: monta um pacote de certificados juntando os
públicos do `certifi` com os instalados no sistema, porque o yt-dlp consulta
apenas o `certifi` e ignora tanto o repositório do Windows quanto a variável
`SSL_CERT_FILE`.

**`No supported JavaScript runtime`** — o YouTube embaralha as URLs de mídia com
um desafio em JavaScript, e resolvê-lo exige um runtime externo. Atenção à
palavra *supported*: o yt-dlp recusa versões antigas (Node abaixo da 22), e um
Node 18 instalado pelo sistema aparece no `PATH` mas é ignorado. Por isso o
`requirements.txt` traz o `nodejs-wheel-binaries`, que instala um Node recente
pelo próprio pip; o app confere a versão antes de usar e informa no painel qual
runtime pegou.

**`HTTP Error 403: Forbidden` no download** — os dados do vídeo chegaram, mas a
URL da mídia foi recusada. O app tenta sozinho vários *player clients* do
YouTube antes de desistir, porque cada um entrega as URLs sob regras diferentes.
Localmente costuma ser temporário.

**`fragment not found; Skipping fragment`** — o YouTube aceitou o pedido e
recusou os fragmentos, um por um. Por padrão o yt-dlp pula os que faltam e
termina "com sucesso", entregando um arquivo sem áudio nenhum; o app desliga
esse comportamento e ainda confere o tamanho do arquivo baixado, de modo que a
tentativa seja descartada e o próximo player client entre em cena, em vez de
transcrever silêncio.

**`requires a GVS PO Token`** — o YouTube passou a exigir um *proof of origin
token* de conexões suspeitas, sobretudo de IPs de datacenter. O yt-dlp não gera
esse token sozinho: precisaria de um provedor externo, que depende de servidor
próprio. Não há contorno pelo app.

## Publicação no Streamlit Community Cloud

O app roda normalmente lá, **mas o download direto do YouTube não funciona** — e
isso não é defeito do código. O YouTube recusa conexões vindas de IPs de
datacenter, que é o caso de qualquer servidor. A recusa aparece de três formas,
todas com a mesma origem: `HTTP 403`, `requires a GVS PO Token` e fragmentos que
retornam "not found". Nenhuma delas tem contorno pelo aplicativo: o yt-dlp não
gera PO Token sozinho, e o provedor externo que geraria precisa de um servidor
Node compilado à parte, que a plataforma não permite.

Por isso, quando roda hospedado, o app já vem com a fonte **Arquivo local**
selecionada: você baixa o vídeo na sua máquina e envia o arquivo. A transcrição
acontece normalmente no servidor. O limite de upload está em 400 MB
(`.streamlit/config.toml`); para vídeos longos, converta para áudio antes, que o
arquivo fica bem menor.

Para que o link do YouTube funcione na nuvem, a única saída é sair por um proxy
residencial. Basta definir `YTDLP_PROXY` nos *secrets* do app, em
`Settings → Secrets`:

```toml
YTDLP_PROXY = "http://usuario:senha@host:porta"
```

Com isso definido, o app volta a aceitar links normalmente. As variáveis
`HTTPS_PROXY` e `HTTP_PROXY` também são respeitadas.

Quanto aos arquivos de implantação: o `packages.txt` instala o `ffmpeg`; o Node
vem pelo `requirements.txt`, no pacote `nodejs-wheel-binaries` — e não pelo
`packages.txt`, porque o `nodejs` do apt do Debian é a versão 18, antiga demais
para o yt-dlp. Mudanças nesses arquivos só valem depois de um **Reboot app** no
painel; um rerun não basta.

## Arquivos

- `app.py` — o aplicativo inteiro (interface e processamento).
- `requirements.txt` — todas as dependências.
- `packages.txt` — pacotes de sistema para o Streamlit Community Cloud.
- `.streamlit/config.toml` — limite de upload.

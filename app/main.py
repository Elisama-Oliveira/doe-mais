"""Doe+ - aplicativo de doações (Flet) - roda no computador, no celular e no navegador."""
import base64
import io
import os
import re
import secrets
import threading
import time

import flet as ft
import requests
from PIL import Image

try:  # fotos HEIC (iPhone), se a biblioteca opcional pillow-heif estiver instalada
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

# chave usada pelo Flet para liberar o envio de fotos pelo navegador
os.environ.setdefault("FLET_SECRET_KEY", secrets.token_hex(16))

# Computador: http://127.0.0.1:8000 | Emulador Android: http://10.0.2.2:8000
# Celular real (mesmo Wi-Fi): http://IP_DO_SEU_PC:8000 | Online: endereço do Render
API = os.environ.get("API_URL", "http://127.0.0.1:8000")
PASTA_UPLOADS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
CATEGORIAS = ["Roupas", "Alimentos", "Móveis", "Eletrônicos", "Livros", "Outros"]
ABAS = ["/inicio", "/doar", "/pedidos", "/perfil"]


def _chamar(token, metodo, caminho, **kw):
    """Chama o servidor. Retorna (dados, erro)."""
    headers = {"Authorization": token} if token else {}
    try:
        r = requests.request(metodo, API + caminho, headers=headers, timeout=15, **kw)
    except requests.RequestException:
        return None, "Sem conexão com o servidor."
    if r.status_code >= 400:
        try:
            msg = r.json().get("detail", "Erro inesperado.")
        except Exception:
            msg = "Erro inesperado."
        return None, msg if isinstance(msg, str) else "Dados inválidos."
    return r.json(), None


def main(page: ft.Page):
    page.title = "Doe+"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 0
    # estado e chamadas ficam dentro de main: cada sessão (aba do navegador) tem o seu login
    estado = {"token": None, "usuario": None, "admin": False, "chat": None, "lista": [],
              "msgs": 0, "pedidos": 0, "foto": None, "receber": None, "situacao": None}
    try:  # lembra do login neste navegador
        if page.client_storage.contains_key("token"):
            estado.update(
                token=page.client_storage.get("token"),
                usuario=page.client_storage.get("usuario"),
                admin=bool(page.client_storage.get("admin")),
            )
    except Exception:
        pass

    def chamar(metodo, caminho, **kw):
        return _chamar(estado["token"], metodo, caminho, **kw)

    corpo = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, spacing=10)
    page.add(ft.Container(corpo, expand=True, padding=15))

    # ---------- utilidades ----------
    def avisar(msg):
        page.open(ft.SnackBar(ft.Text(msg)))

    def mostrar(titulo, *controles):
        corpo.controls = [ft.Text(titulo, size=22, weight=ft.FontWeight.BOLD), *controles]
        page.update()

    def foto_url(item_id):
        return API + "/itens/" + str(item_id) + "/foto"

    def miniatura(i):
        if not i.get("tem_foto"):
            return None
        return ft.Image(src=foto_url(i["id"]), width=56, height=56, fit=ft.ImageFit.COVER, border_radius=8)

    def cartao(titulo, sub, acoes=None, ao_tocar=None, leading=None):
        itens = [ft.ListTile(leading=leading, title=ft.Text(titulo), subtitle=ft.Text(sub), on_click=ao_tocar)]
        if acoes:
            itens.append(ft.Row(acoes, wrap=True))
        return ft.Card(ft.Container(ft.Column(itens), padding=5))

    def whatsapp(telefone):
        numero = re.sub(r"\D", "", telefone)
        if len(numero) <= 11:
            numero = "55" + numero
        page.launch_url("https://wa.me/" + numero)

    # ---------- navegação (o botão voltar do navegador funciona) ----------
    def barra():
        if page.navigation_bar is None:
            page.navigation_bar = ft.NavigationBar(
                destinations=[
                    ft.NavigationBarDestination(icon=ft.Icons.HOME, label="Início"),
                    ft.NavigationBarDestination(icon=ft.Icons.ADD_CIRCLE, label="Doar"),
                    ft.NavigationBarDestination(icon=ft.Icons.INBOX, label="Pedidos"),
                    ft.NavigationBarDestination(icon=ft.Icons.PERSON, label="Perfil"),
                ],
                on_change=lambda e: page.go(ABAS[e.control.selected_index]),
            )

    def aba_da_rota(rota):
        if rota.startswith("/doar"):
            return 1
        if rota.startswith(("/pedidos", "/chat")):
            return 2
        if rota.startswith(("/perfil", "/admin")):
            return 3
        return 0

    def desenhar(e=None):
        """Mostra a tela da rota atual. Chamado ao navegar e ao usar voltar/avançar do navegador."""
        rota = page.route or "/"
        estado["chat"] = None
        if rota == "/cadastro":
            return tela_cadastro()
        if not estado["token"]:
            return tela_login()
        barra()
        page.navigation_bar.selected_index = aba_da_rota(rota)
        partes = rota.split("/")
        if rota.startswith("/chat/") and partes[2].isdigit():
            return tela_chat(int(partes[2]))
        if rota.startswith("/item/"):
            item = next((i for i in estado["lista"] if str(i["id"]) == partes[2]), None)
            return tela_detalhes(item) if item else tela_inicio()
        if rota == "/admin" and estado["admin"]:
            return tela_admin()
        {"/doar": tela_doar, "/pedidos": tela_pedidos, "/perfil": tela_perfil}.get(rota, tela_inicio)()

    # ---------- avisos de novas mensagens e pedidos ----------
    def vigiar():
        token = estado["token"]
        while estado["token"] == token:
            time.sleep(4)
            try:
                d, erro = chamar("GET", "/novidades")
                if erro or estado["token"] != token:
                    continue
                msgs = d["mensagens"] - d["por_chat"].get(str(estado["chat"]), 0)
                total = msgs + d["pedidos"]
                if page.navigation_bar:
                    page.navigation_bar.destinations[2].label = "Pedidos (" + str(total) + ")" if total else "Pedidos"
                if msgs > estado["msgs"]:
                    avisar("Você recebeu uma nova mensagem!")
                elif d["pedidos"] > estado["pedidos"]:
                    avisar("Você recebeu um novo pedido de doação!")
                estado["msgs"], estado["pedidos"] = msgs, d["pedidos"]
                page.update()
            except Exception:
                break  # a aba foi fechada

    def iniciar_vigia():
        estado["msgs"] = estado["pedidos"] = 0
        threading.Thread(target=vigiar, daemon=True).start()

    # ---------- login e cadastro ----------
    def tela_login(e=None):
        page.navigation_bar = None
        usuario = ft.TextField(label="Usuário")
        senha = ft.TextField(label="Senha", password=True, can_reveal_password=True)

        def entrar(e):
            d, erro = chamar("POST", "/login", json={"usuario": usuario.value or "", "senha": senha.value or ""})
            if erro:
                return avisar(erro)
            estado.update(token=d["token"], usuario=d["usuario"], admin=d["admin"])
            try:
                page.client_storage.set("token", d["token"])
                page.client_storage.set("usuario", d["usuario"])
                page.client_storage.set("admin", d["admin"])
            except Exception:
                pass
            iniciar_vigia()
            page.go("/inicio")

        mostrar(
            "Doe+",
            usuario,
            senha,
            ft.ElevatedButton("Entrar", on_click=entrar),
            ft.TextButton("Criar conta", on_click=lambda e: page.go("/cadastro")),
        )

    def tela_cadastro(e=None):
        page.navigation_bar = None
        usuario = ft.TextField(label="Usuário")
        telefone = ft.TextField(
            label="Telefone/WhatsApp com DDD (obrigatório)", keyboard_type=ft.KeyboardType.PHONE
        )
        senha = ft.TextField(label="Senha", password=True, can_reveal_password=True)
        confirmar = ft.TextField(label="Confirmar senha", password=True, can_reveal_password=True)

        def criar(e):
            digitos = re.sub(r"\D", "", telefone.value or "")
            if len(digitos) < 10:
                return avisar("Informe um telefone válido, com DDD.")
            if senha.value != confirmar.value:
                return avisar("As senhas não conferem.")
            d, erro = chamar(
                "POST", "/cadastro", json={"usuario": usuario.value or "", "senha": senha.value or "", "telefone": digitos}
            )
            if erro:
                return avisar(erro)
            avisar("Conta criada! Faça login.")
            page.go("/login")

        mostrar(
            "Criar conta",
            usuario,
            telefone,
            senha,
            confirmar,
            ft.ElevatedButton("Cadastrar", on_click=criar),
            ft.TextButton("Voltar", on_click=lambda e: page.go("/login")),
        )

    # ---------- início: lista de doações ----------
    def tela_inicio():
        busca = ft.TextField(label="Buscar item", expand=True)
        cat = ft.Dropdown(
            label="Categoria",
            options=[ft.dropdown.Option("Todas")] + [ft.dropdown.Option(c) for c in CATEGORIAS],
            value="Todas",
        )
        lista = ft.Column()

        def carregar(e=None):
            params = {"busca": busca.value or ""}
            if cat.value != "Todas":
                params["categoria"] = cat.value
            d, erro = chamar("GET", "/itens", params=params)
            if erro:
                return avisar(erro)
            estado["lista"] = d
            lista.controls = [
                cartao(
                    i["titulo"],
                    i["categoria"] + " • " + ("seu item" if i["doador"] == estado["usuario"] else "doador: " + i["doador"])
                    + (" • reservado" if i["status"] == "reservado" else ""),
                    ao_tocar=lambda e, n=i["id"]: page.go("/item/" + str(n)),
                    leading=miniatura(i),
                )
                for i in d
            ] or [ft.Text("Nenhum item disponível no momento.")]
            page.update()

        cat.on_change = carregar
        mostrar(
            "Itens para doação",
            ft.Row([busca, ft.IconButton(ft.Icons.SEARCH, on_click=carregar)]),
            cat,
            lista,
        )
        carregar()

    def tela_detalhes(item):
        msg = ft.TextField(label="Mensagem para o doador (opcional)", multiline=True)

        def solicitar(e):
            d, erro = chamar("POST", "/itens/" + str(item["id"]) + "/solicitar", json={"mensagem": msg.value or ""})
            if erro:
                return avisar(erro)
            avisar("Solicitação enviada!")
            page.go("/pedidos")

        def remover(e):
            chamar("DELETE", "/itens/" + str(item["id"]))
            page.go("/inicio")

        proprio = item["doador"] == estado["usuario"]  # o doador não solicita o próprio item
        botoes = [] if proprio else [ft.ElevatedButton("Solicitar item", on_click=solicitar)]
        botoes.append(ft.TextButton("Voltar", on_click=lambda e: page.go("/inicio")))
        if estado["admin"]:
            botoes.append(ft.OutlinedButton("Remover item (admin)", on_click=remover))
        foto = []
        if item.get("tem_foto"):
            foto = [ft.Image(src=foto_url(item["id"]), width=300, fit=ft.ImageFit.CONTAIN, border_radius=8)]
        mostrar(
            item["titulo"],
            *foto,
            ft.Text("Categoria: " + item["categoria"]),
            ft.Text("Doador: " + item["doador"]),
            ft.Text(item["descricao"] or "Sem descrição."),
            *([ft.Text("Este é o seu item." + (" Já está reservado: veja em Pedidos." if item["status"] == "reservado" else ""))] if proprio else [msg]),
            ft.Row(botoes, wrap=True),
        )

    # ---------- doar (com foto) ----------
    def preparar_foto(dados):
        """Reduz a foto (máx. 900 px) e converte para JPEG, para ficar leve."""
        img = Image.open(io.BytesIO(dados)).convert("RGB")
        img.thumbnail((900, 900))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()

    def ao_escolher(e: ft.FilePickerResultEvent):
        if not e.files:
            return
        arquivo = e.files[0]
        if page.web:  # no navegador, a foto é enviada primeiro para a pasta uploads
            estado["situacao"].value = "Enviando foto..."
            page.update()
            try:
                picker.upload(
                    [ft.FilePickerUploadFile(arquivo.name, upload_url=page.get_upload_url(arquivo.name, 120))]
                )
            except Exception as erro:
                estado["situacao"].value = "Falha ao enviar a foto."
                page.update()
                avisar("Falha no envio da foto: " + str(erro))
        else:
            with open(arquivo.path, "rb") as f:
                estado["receber"](f.read())

    def ao_enviar(e: ft.FilePickerUploadEvent):
        if e.error:
            estado["situacao"].value = "Falha ao enviar a foto."
            page.update()
            return avisar("Falha ao enviar a foto: " + str(e.error))
        if e.progress == 1.0:
            caminho = os.path.join(PASTA_UPLOADS, e.file_name)
            try:
                with open(caminho, "rb") as f:
                    dados = f.read()
                os.remove(caminho)
            except OSError:
                return avisar("Falha ao ler a foto enviada.")
            estado["receber"](dados)

    picker = ft.FilePicker(on_result=ao_escolher, on_upload=ao_enviar)
    page.overlay.append(picker)

    def tela_doar():
        estado["foto"] = None
        titulo = ft.TextField(label="O que você quer doar?")
        desc = ft.TextField(label="Descrição", multiline=True)
        cat = ft.Dropdown(label="Categoria", options=[ft.dropdown.Option(c) for c in CATEGORIAS], value=CATEGORIAS[0])
        previa = ft.Column()
        situacao = ft.Text("Nenhuma foto escolhida", size=12)
        estado["situacao"] = situacao

        def receber(dados):
            try:
                estado["foto"] = preparar_foto(dados)
            except Exception:
                return avisar("Não consegui ler essa imagem. Use uma foto JPG ou PNG.")
            previa.controls = [
                ft.Image(src_base64=estado["foto"], width=160, height=160, fit=ft.ImageFit.COVER, border_radius=8)
            ]
            situacao.value = "Foto selecionada"
            page.update()

        estado["receber"] = receber

        def publicar(e):
            if not (titulo.value or "").strip():
                return avisar("Informe o título.")
            d, erro = chamar(
                "POST",
                "/itens",
                json={
                    "titulo": titulo.value,
                    "descricao": desc.value or "",
                    "categoria": cat.value,
                    "foto_b64": estado["foto"] or "",
                },
            )
            if erro:
                return avisar(erro)
            avisar("Item publicado!")
            tela_doar()

        def excluir(item_id):
            chamar("DELETE", "/itens/" + str(item_id))
            tela_doar()

        itens, _ = chamar("GET", "/meus-itens")
        meus = [
            cartao(
                i["titulo"],
                i["categoria"] + " • " + i["status"],
                [ft.TextButton("Excluir", on_click=lambda e, n=i["id"]: excluir(n))],
                leading=miniatura(i),
            )
            for i in itens or []
        ] or [ft.Text("Você ainda não cadastrou itens.")]
        mostrar(
            "Doar um item",
            titulo,
            desc,
            cat,
            ft.OutlinedButton(
                "Escolher foto",
                icon=ft.Icons.PHOTO_CAMERA,
                on_click=lambda e: picker.pick_files(allow_multiple=False, file_type=ft.FilePickerFileType.IMAGE),
            ),
            situacao,
            previa,
            ft.ElevatedButton("Publicar", on_click=publicar),
            ft.Divider(),
            ft.Text("Meus itens", weight=ft.FontWeight.BOLD),
            *meus,
        )

    # ---------- pedidos ----------
    def tela_pedidos():
        recebidos, _ = chamar("GET", "/pedidos-recebidos")
        feitos, _ = chamar("GET", "/minhas-solicitacoes")
        nov, _ = chamar("GET", "/novidades")
        nao_lidas = nov["por_chat"] if nov else {}

        def botao_chat(s):
            n = nao_lidas.get(str(s["id"]), 0)
            texto = "Chat (" + str(n) + " nova" + ("s" if n > 1 else "") + ")" if n else "Chat"
            return ft.TextButton(texto, on_click=lambda e, i=s["id"]: page.go("/chat/" + str(i)))

        def acao(caminho):
            def f(e):
                _, erro = chamar("POST", caminho)
                if erro:
                    avisar(erro)
                tela_pedidos()

            return f

        blocos = [ft.Text("Pedidos recebidos", size=16, weight=ft.FontWeight.BOLD)]
        for s in recebidos or []:
            botoes = [botao_chat(s)]
            if s["status"] == "pendente":
                botoes += [
                    ft.ElevatedButton("Aceitar", on_click=acao("/solicitacoes/" + str(s["id"]) + "/aceitar")),
                    ft.OutlinedButton("Recusar", on_click=acao("/solicitacoes/" + str(s["id"]) + "/recusar")),
                ]
            if s["status"] == "aceita":
                if s["telefone"]:
                    botoes.append(ft.TextButton("WhatsApp", on_click=lambda e, t=s["telefone"]: whatsapp(t)))
                botoes.append(
                    ft.ElevatedButton("Doação concluída", on_click=acao("/itens/" + str(s["item_id"]) + "/concluir"))
                )
                botoes.append(
                    ft.OutlinedButton("Cancelar doação", on_click=acao("/solicitacoes/" + str(s["id"]) + "/cancelar"))
                )
            blocos.append(
                cartao(
                    s["titulo"],
                    "Pedido de " + s["receptor"] + " • " + s["status"] + "\n" + s["mensagem"],
                    botoes,
                    leading=miniatura({"id": s["item_id"], "tem_foto": s["tem_foto"]}),
                )
            )
        if not recebidos:
            blocos.append(ft.Text("Nenhum pedido recebido."))

        blocos += [ft.Divider(), ft.Text("Minhas solicitações", size=16, weight=ft.FontWeight.BOLD)]
        for s in feitos or []:
            botoes = [botao_chat(s)]
            if s["status"] == "aceita":
                if s["telefone"]:
                    botoes.append(ft.TextButton("WhatsApp", on_click=lambda e, t=s["telefone"]: whatsapp(t)))
                botoes.append(
                    ft.OutlinedButton("Desistir", on_click=acao("/solicitacoes/" + str(s["id"]) + "/cancelar"))
                )
            blocos.append(
                cartao(
                    s["titulo"],
                    "Doador: " + s["doador"] + " • " + s["status"],
                    botoes,
                    leading=miniatura({"id": s["item_id"], "tem_foto": s["tem_foto"]}),
                )
            )
        if not feitos:
            blocos.append(ft.Text("Você ainda não solicitou nenhum item."))
        mostrar("Pedidos", *blocos)

    # ---------- chat ----------
    def tela_chat(sol_id):
        estado["chat"] = sol_id
        msgs = ft.ListView(height=380, spacing=6, auto_scroll=True)
        cabecalho = ft.Text("", size=16)
        texto = ft.TextField(label="Mensagem", expand=True)

        def carregar():
            d, erro = chamar("GET", "/chat/" + str(sol_id))
            if erro:
                return
            cabecalho.value = "Conversa com " + d["outro"]
            msgs.controls = [
                ft.Row(
                    [
                        ft.Container(
                            ft.Column(
                                [ft.Text(m["texto"]), ft.Text(m["criado"][11:16], size=10)],
                                spacing=2,
                                tight=True,
                            ),
                            bgcolor=ft.Colors.GREEN_100 if m["minha"] else ft.Colors.GREY_200,
                            padding=8,
                            border_radius=8,
                        )
                    ],
                    alignment=ft.MainAxisAlignment.END if m["minha"] else ft.MainAxisAlignment.START,
                )
                for m in d["mensagens"]
            ]
            page.update()

        def enviar(e=None):
            if not (texto.value or "").strip():
                return
            _, erro = chamar("POST", "/chat/" + str(sol_id), json={"texto": texto.value})
            if erro:
                return avisar(erro)
            texto.value = ""
            carregar()

        texto.on_submit = enviar

        def atualizar_sozinho():
            while estado["chat"] == sol_id:
                time.sleep(3)
                try:
                    if estado["chat"] == sol_id:
                        carregar()
                except Exception:
                    break

        mostrar(
            "Chat",
            cabecalho,
            msgs,
            ft.Row([texto, ft.IconButton(ft.Icons.SEND, on_click=enviar)]),
            ft.TextButton("Voltar", on_click=lambda e: page.go("/pedidos")),
        )
        carregar()
        threading.Thread(target=atualizar_sozinho, daemon=True).start()

    # ---------- perfil e administrador ----------
    def sair(e=None):
        estado.update(token=None, usuario=None, admin=False, chat=None)
        try:
            for k in ("token", "usuario", "admin"):
                page.client_storage.remove(k)
        except Exception:
            pass
        page.navigation_bar = None
        page.go("/login")

    def tela_perfil():
        nome = str(estado["usuario"] or "")
        meus, _ = chamar("GET", "/meus-itens")
        recebidos, _ = chamar("GET", "/pedidos-recebidos")
        feitos, _ = chamar("GET", "/minhas-solicitacoes")

        def numero(lista, rotulo):
            return ft.Container(
                ft.Column(
                    [
                        ft.Text(str(len(lista or [])), size=26, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_700),
                        ft.Text(rotulo, size=12, text_align=ft.TextAlign.CENTER),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=2,
                ),
                expand=True,
                padding=12,
                border_radius=12,
                bgcolor=ft.Colors.GREEN_50,
            )

        botoes = [ft.ElevatedButton("Sair", icon=ft.Icons.LOGOUT, on_click=sair, width=320)]
        if estado["admin"]:
            botoes.insert(
                0,
                ft.ElevatedButton(
                    "Painel do administrador", icon=ft.Icons.ADMIN_PANEL_SETTINGS, on_click=lambda e: page.go("/admin"), width=320
                ),
            )
        mostrar(
            "Perfil",
            ft.Column(
                [
                    ft.CircleAvatar(
                        content=ft.Text(nome[:1].upper(), size=44, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                        radius=55,
                        bgcolor=ft.Colors.GREEN_600,
                    ),
                    ft.Text(nome, size=32, weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER),
                    ft.Container(
                        ft.Text("Administrador" if estado["admin"] else "Membro do Doe+", size=13, color=ft.Colors.GREEN_800),
                        bgcolor=ft.Colors.GREEN_50,
                        padding=ft.padding.symmetric(horizontal=14, vertical=4),
                        border_radius=20,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
            ft.Row(
                [numero(meus, "Itens para doar"), numero(recebidos, "Pedidos recebidos"), numero(feitos, "Minhas solicitações")],
                spacing=8,
            ),
            ft.Column(botoes, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        )

    def tela_admin():
        usuarios, erro = chamar("GET", "/admin/usuarios")
        if erro:
            return avisar(erro)

        def agir(metodo, caminho):
            chamar(metodo, caminho)
            tela_admin()

        cartoes = []
        for u in usuarios:
            if u["admin"]:
                continue
            cartoes.append(
                cartao(
                    u["usuario"],
                    "BLOQUEADO" if u["bloqueado"] else "Ativo",
                    [
                        ft.TextButton(
                            "Desbloquear" if u["bloqueado"] else "Bloquear",
                            on_click=lambda e, n=u["id"]: agir("POST", "/admin/usuarios/" + str(n) + "/bloquear"),
                        ),
                        ft.TextButton(
                            "Excluir", on_click=lambda e, n=u["id"]: agir("DELETE", "/admin/usuarios/" + str(n))
                        ),
                    ],
                )
            )
        mostrar(
            "Administrador",
            *(cartoes or [ft.Text("Nenhum usuário cadastrado.")]),
            ft.TextButton("Voltar", on_click=lambda e: page.go("/perfil")),
        )

    page.on_route_change = desenhar
    if estado["token"]:
        iniciar_vigia()
    desenhar()


if __name__ == "__main__":
    import sys

    os.makedirs(PASTA_UPLOADS, exist_ok=True)
    if "web" in sys.argv:  # python app/main.py web  -> site em http://127.0.0.1:8550 (acrescente "rede" para abrir no celular)
        ft.app(
            target=main,
            view=ft.AppView.WEB_BROWSER,
            port=int(os.environ.get("PORT", 8550)),
            host="0.0.0.0" if ("PORT" in os.environ or "rede" in sys.argv) else "127.0.0.1",
            upload_dir=PASTA_UPLOADS,
        )
    else:  # python app/main.py  -> janela de computador
        ft.app(target=main, upload_dir=PASTA_UPLOADS)
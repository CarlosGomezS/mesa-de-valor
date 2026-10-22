# Mesa de Valor

Web privada en GitHub Pages y un bot que se ejecuta solo cada hora con GitHub Actions.

- **Ficticio:** apuesta dinero de mentira con cuotas reales y lleva la cuenta.
- **Real:** te avisa por Telegram de dónde apostar, a qué cuota y cuánto. Cuando la haces, pulsas «La he hecho» en la web y el bot la sigue y la cobra solo.
- Todos los datos (ajustes, claves y apuestas) se guardan **cifrados** con tu clave. La web es pública, pero sin la clave solo se ve texto ilegible.

## Puesta en marcha

1. Este repositorio tiene que ser **público** (GitHub Pages gratis lo exige; los datos van cifrados).
2. Settings → Pages → Source: *Deploy from a branch* → Branch `main`, carpeta `/docs` → Save.
3. Abre `https://TU-USUARIO.github.io/NOMBRE-DEL-REPO/` y sigue la configuración inicial (clave, token de GitHub, clave de cuotas y Telegram).

A partir de ahí todo se maneja desde la web.

## Cómo decide

Probabilidad real = cuotas de Pinnacle sin margen (método de potencia). Solo propone una apuesta si otra casa paga al menos un 3 % más de lo que vale (perfil prudente). El importe es ¼ de Kelly con tope del 4 % de la banca. Para solo si pierde un 10 % en un día, un 20 % en una semana o un 40 % en total. Mide el CLV de cada apuesta: si de media no batimos la cuota de cierre, el método no tiene ventaja.

## Pruebas

`pip install cryptography && python -m unittest discover -s tests`

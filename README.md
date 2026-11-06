# Restauración histórica de SQL Server en Linux Mint 22

Entorno local para inspeccionar y recuperar hasta tres bases independientes con **SQL Server 2022 Developer**. Los originales se conservan y el contenedor los recibe en solo lectura. Ningún inicio de contenedor ejecuta restauraciones. **`make restore` constituye una orden explícita de restaurar** el plan seleccionado; nunca sobrescribe bases existentes.

El proyecto está creado directamente en este directorio. Se añadió `scripts/historico.py` como núcleo común: usa únicamente la biblioteca estándar de Python para manejar JSON, hashes, escapado SQL, bloqueos y reportes sin analizar tablas de texto ni depender de `jq`/controladores Python. Los puntos de entrada solicitados siguen siendo scripts Bash.

## Requisitos

- Linux x86_64 nativo, al menos 2 CPU y 2 GiB de RAM disponibles; recomendados 6 GiB disponibles o más. El motor tiene un límite interno de memoria de 4096 MB; no es un límite absoluto de memoria del contenedor.
- Docker Engine y Docker Compose con `docker compose`, Bash, Python 3.10+ y GNU Make.
- Al menos 6 GiB libres para iniciar, además del tamaño **sin comprimir de todos los archivos de datos y log**. La restauración comprueba el volumen real y exige tamaño original + 20% + 1 GiB.
- Acceso al daemon Docker con su usuario. Pertenecer al grupo `docker` equivale a tener amplios privilegios sobre el equipo; configure ese acceso según su política local.
- TCP 1433 disponible. Si pertenece a este proyecto y está publicado en localhost, se admite reiniciar el mismo entorno.
- Internet para descargar la imagen oficial y, solamente si hace falta, construir el cliente ODBC 18. Los respaldos no se transmiten a ningún servicio externo.

SQL Server en contenedores requiere anfitrión Intel/AMD x86_64; no se fuerza emulación ARM. Véase la [guía de instalación de Microsoft](https://learn.microsoft.com/en-us/sql/linux/install-upgrade/setup?view=sql-server-ver16).

## Archivos

```text
compose.yaml                  motor oficial, volumen y red local
tools.Dockerfile               alternativa con mssql-tools18 y ODBC 18
.env / .env.example            secreto local / ejemplo sin secreto
Makefile                      administración
restore-plan.json.example     selección explícita de conjuntos y destinos
backups/                      copias locales de .bak, sin enlaces simbólicos
scripts/*.sh                  comandos Bash
scripts/historico.py           lógica compartida, sin paquetes Python externos
sql/header.sql                HEADERONLY convertido a JSON
sql/filelist.sql              FILELISTONLY convertido a JSON
sql/consulta-ejemplo.sql       consulta de catálogo
sql/generated/                RESTORE generados al invocar make restore
reports/state.json            evidencia técnica local y huellas SHA-256
reports/informe-restauracion.md informe en español
tests/test_safety.py           barreras de seguridad sin Docker
```

`reports/`, `.env`, respaldos, bases físicas y SQL generado están excluidos de Git. Los reportes se escriben con permisos privados. No se leen filas de las tablas de usuario; se omiten usuario, máquina y descripción libre del encabezado. Las rutas de perfiles personales se ocultan en el informe Markdown. Los nombres de bases, nombres lógicos y rutas originales siguen siendo información institucional: mantenga todo el directorio en almacenamiento local protegido.

## Instalación de Docker, si falta

Los scripts solo diagnostican; no instalan paquetes ni ejecutan `sudo`. Linux Mint 22 usa Ubuntu 24.04 **noble**, no el nombre de versión propio de Mint. Docker advierte que Mint es una distribución derivada sin soporte oficial directo. Consulte las [instrucciones oficiales para Ubuntu](https://docs.docker.com/engine/install/ubuntu/) y revise paquetes en conflicto antes de instalar.

Estos comandos modifican el sistema y son para ejecución manual del administrador:

```bash
sudo apt-get update
sudo apt-get install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nSigned-By: /etc/apt/keyrings/docker.asc\nArchitectures: %s\n' \
  "${UBUNTU_CODENAME:-$VERSION_CODENAME}" "$(dpkg --print-architecture)" \
  | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
sudo apt-get update
sudo apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Si hay `docker.io`, `podman-docker`, `containerd` u otros paquetes incompatibles, evalúe su sustitución conforme a la documentación oficial. No desinstale automáticamente servicios en uso. Una instalación reciente puede necesitar que el administrador autorice el acceso al socket y que el usuario cierre y abra sesión. No use `chmod 666 /var/run/docker.sock`.

## Configurar credenciales

En esta implementación ya se ha creado un `.env` privado con una contraseña aleatoria única. No se imprime ni se guarda en reportes. En una copia nueva del proyecto:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Use exactamente estas dos claves:

```dotenv
ACCEPT_EULA=Y
MSSQL_SA_PASSWORD='SU_SECRETO_UNICO_DE_16_A_128_CARACTERES'
```

Reemplace el texto de ejemplo por un secreto de 16–128 caracteres y al menos tres clases entre mayúsculas, minúsculas, números y símbolos. Mantenga las comillas simples, no use comillas simples dentro del secreto ni saltos de línea. No hay interpolación ni ejecución del archivo como Bash; `$` y `#` entre comillas simples se conservan literalmente. `ACCEPT_EULA=Y` expresa su aceptación de los términos de Microsoft. Developer es para desarrollo/pruebas y análisis local, no producción.

No publique `docker compose config` sin `--quiet`: su salida expandida contiene secretos. `make config` solo valida y no los muestra. Los scripts excluyen sustituciones de las credenciales por variables del shell. La contraseña llega a sqlcmd mediante `SQLCMDPASSWORD`, nunca con `-P` ni argumentos con el valor del secreto. Docker y sus administradores pueden acceder al entorno del contenedor: proteja el acceso al daemon.

**Persistencia:** cambiar `.env` después del primer inicio no cambia la contraseña que SQL Server conserva en `master`. Si ocurre, restaure en `.env` el valor vigente o gestione una rotación explícita con un DBA; no borre el volumen para resolverlo.

## Copiar los respaldos y permitir su lectura

Copie los tres archivos a `backups/`. No modifique los originales de origen ni trabaje con enlaces simbólicos. Se aceptan extensiones `.bak` sin distinguir mayúsculas y nombres con espacios/apóstrofos; los nombres con controles se rechazan.

```bash
cp --preserve=timestamps -- /ruta/original/fiscalia2015.bak backups/
cp --preserve=timestamps -- /ruta/original/fiscalia2016.bak backups/
cp --preserve=timestamps -- /ruta/original/fiscalia2017.bak backups/
sha256sum /ruta/original/fiscalia2015.bak backups/fiscalia2015.bak
sha256sum /ruta/original/fiscalia2016.bak backups/fiscalia2016.bak
sha256sum /ruta/original/fiscalia2017.bak backups/fiscalia2017.bak
```

Cada pareja debe tener el mismo SHA-256. Los scripts calculan huellas antes/después de inspeccionar, verificar y restaurar; un cambio invalida las etapas previas. No copie ni reemplace un respaldo mientras haya una operación en marcha: el montaje de solo lectura protege frente al contenedor, no frente a otros procesos del anfitrión.

El usuario del motor oficial suele ser UID **10001**. Compruébelo tras `make up`:

```bash
docker compose exec -T sqlserver id
```

Si las copias no son legibles, puede conceder una ACL al UID real sobre **las copias y su directorio**, sin dar acceso general al resto de usuarios. Si `setfacl` falta, el administrador puede instalar el paquete `acl` manualmente. Para UID 10001:

```bash
chmod 750 backups
chmod 640 backups/*.bak
setfacl -m u:10001:rx backups
setfacl -m u:10001:r backups/*.bak
docker compose exec -T sqlserver test -r /var/opt/mssql/backup/fiscalia2015.bak
```

Repita `test -r` para 2016 y 2017; `make up` también comprueba todas las copias existentes. Esto no cambia bytes del respaldo. No ejecute los comandos sobre los originales externos. Con Docker rootless o remapeo de usuarios, el UID del anfitrión cambia: solicite al administrador la ACL correspondiente. No ejecute el motor como root ni use permisos 777.

## Flujo de trabajo

```bash
make check       # distribución, CPU/RAM/disco, Docker, puerto, permisos, hashes
make config      # validar Compose sin mostrar credenciales
make up          # descargar/iniciar y esperar una consulta SQL real; SIN restaurar
make status
make inspect     # HEADERONLY de todos los archivos; FILELISTONLY de cada FILE
make verify      # VERIFYONLY de cada conjunto con FILE explícito
make report      # revisar metadatos, versiones, fechas, tipos y limitaciones
```

Las fechas y el nombre originales se obtienen del encabezado; no se presupone que `fiscalia2015.bak` contenga una base de 2015 ni que provenga de SQL Server 2014. Un origen 2014 debe indicar `SoftwareVersionMajor=12`. Cada conjunto muestra tamaño de respaldo y suma de archivos originales, nombres lógicos, rutas, checksums y señales de cifrado/TDE.

`VERIFYONLY` usa `CHECKSUM` cuando el encabezado declara checksums y siempre `STOP_ON_ERROR`. No usa `CONTINUE_AFTER_ERROR`. Si no hay checksums, se documenta la cobertura reducida. Se generan rutas Linux con `MOVE` para verificar archivos ordinarios sin depender de las rutas Windows originales. Una verificación correcta acredita legibilidad preliminar; la [documentación de VERIFYONLY](https://learn.microsoft.com/en-us/sql/t-sql/statements/restore-statements-verifyonly-transact-sql?view=sql-server-ver16) explica sus límites. La restauración y CHECKDB siguen siendo obligatorios.

Si hay varios conjuntos en un archivo, ningún conjunto se elige automáticamente. Copie y edite el plan **después de inspeccionar las posiciones reales**:

```bash
cp restore-plan.json.example restore-plan.json
nano restore-plan.json
```

Ejemplo de selección deliberada (solo si el informe confirma esa posición):

```json
{
  "selections": [
    {"file": "fiscalia2015.bak", "position": 2, "target": "fiscalia2015"}
  ]
}
```

El plan admite uno a tres conjuntos y destinos distintos. Si omite un archivo del plan, ese archivo no se restaura. Sin plan, solo se aceptan archivos con los tres nombres esperados y **exactamente un conjunto por archivo**. Los nombres de destino predeterminados son `fiscalia2015`, `fiscalia2016` y `fiscalia2017`. Nombres desconocidos siempre requieren plan explícito.

Cuando haya revisado los resultados y quiera restaurar, ejecute usted:

```bash
make restore     # ORDEN EXPLÍCITA: crea bases nuevas, secuencialmente
make validate    # existencia, ONLINE, compatibilidad, tamaños, objetos y CHECKDB
make report
```

La restauración exige inspección y verificación del **mismo hash y posición**. Mueve todos los MDF/NDF/LDF a nombres únicos en el volumen, muestra `STATS`, captura errores SQL y registra cada intento antes de iniciarlo. La primera colisión conocida detiene todo el lote antes de crear bases; cada sentencia vuelve a comprobar `DB_ID`. También se rechazan archivos físicos de destino preexistentes. Tras un fallo de restauración, se detiene el resto del lote y se conserva el diagnóstico; no se intenta limpiar automáticamente.

Si una base ya existe, debe decidir explícitamente un destino nuevo en el plan. No hay comandos automáticos `DROP DATABASE`, `WITH REPLACE`, reparación DBCC ni eliminación de volúmenes. Las operaciones del proyecto se serializan con un bloqueo; evite cambios paralelos manuales en SQL Server durante una restauración.

Las rutas automáticas admiten archivos ordinarios `D` y `L`, incluidos múltiples datos/logs. Se inspeccionan y se intentan verificar otros tipos, pero se bloquea su restauración automática. Se bloquean también respaldos diferenciales, logs, parciales, dañados, snapshots, cifrados/TDE o de versiones superiores. Estos casos pueden necesitar otros medios, cadenas de LSN, certificados con clave privada o un proceso DBA separado. No se importan certificados ni se adivinan cadenas o contraseñas. Un respaldo dividido en varios archivos no se reúne automáticamente: VERIFYONLY registrará los medios faltantes.

## Validación y reportes

`make validate` comprueba siempre las tres bases esperadas y también destinos personalizados registrados. Si se restaura solo una base, las restantes se informan como ausentes y el comando devuelve error: no se disimula una recuperación parcial. Consulta únicamente catálogos: estado, compatibilidad, tamaños, cantidad de tablas/vistas/procedimientos de usuario. Ejecuta **DBCC CHECKDB completo**, sin `PHYSICAL_ONLY` ni reparaciones. Para no exponer datos de páginas dañadas, guarda códigos de error DBCC, sin su salida cruda. No modifica el nivel de compatibilidad.

El informe incluye fecha con zona America/La_Paz, equipo, versiones, imagen/digest del motor, inventario, metadatos por FILE, verificaciones, intentos, validaciones y limitaciones. Cada fallo operativo también regenera el informe. `make report` funciona sin Docker ni `.env`. Los estados tienen fecha y reflejan la última ejecución; no certifican que una base no haya cambiado después. Reejecute validación después de cualquier cambio relevante.

SQL Server 2022 puede actualizar el formato interno de la base restaurada. **Un respaldo nuevo de esa base no se puede devolver directamente a SQL Server 2014.** Conserve los originales. CHECKDB no certifica reglas de negocio, integridad semántica de datos o funcionamiento de aplicaciones: esos análisis son posteriores.

## sqlcmd y consultas manuales

Se comprueba que exista `/opt/mssql-tools18/bin/sqlcmd` antes de usarlo. El cliente exige cifrado con `-N` y acepta el certificado autofirmado local con `-C`. Esto confía en el servidor local sin validar una cadena pública; para otro entorno, configure certificados confiables.

```bash
make sql FILE=sql/consulta-ejemplo.sql
# También desde Bash:
./scripts/sqlcmd.sh sql/consulta-ejemplo.sql
```

Los archivos SQL se ejecutan con los privilegios de `sa`; use únicamente scripts revisados. Evite consultas con datos personales si piensa compartir su salida. Los scripts internos desactivan sustitución de variables sqlcmd y comandos peligrosos del cliente.

Si la imagen no trae herramientas ODBC 18:

```bash
make tools
./scripts/wait-for-sqlserver.sh
make inspect
```

El cliente alternativo se construye con Ubuntu oficial y paquetes firmados de Microsoft `mssql-tools18`/`msodbcsql18`; se conecta a `sqlserver:1433` en la red privada. El motor sigue siendo exclusivamente la imagen oficial solicitada. Las [instrucciones de herramientas Microsoft](https://learn.microsoft.com/en-us/sql/linux/install-upgrade/setup-tools?view=sql-server-ver16) describen los paquetes utilizados. `make tools` requiere acceso a los repositorios oficiales.

El healthcheck del contenedor verifica una consulta real cuando el motor incluye sqlcmd 18; si falta, marca `unhealthy` en lugar de afirmar disponibilidad. En ese caso el script de espera verifica disponibilidad real mediante el cliente alternativo, aunque Docker siga indicando `unhealthy`. No use ese estado para borrar/recrear datos. Con la imagen probada, las herramientas están incorporadas.

## DBeaver en Linux Mint

Configure una conexión Microsoft SQL Server con host `127.0.0.1`, puerto `1433`, usuario `sa`, contraseña leída localmente de `.env` y base `fiscalia2015` (o `master` antes de restaurar). En propiedades del controlador use `encrypt=true` y `trustServerCertificate=true` para el certificado local. Repita con las otras bases. Introduzca el secreto en la interfaz, no en una URL guardada en informes. Para análisis continuado, solicite a un DBA un usuario de solo lectura en lugar de distribuir `sa`.

## SSMS en Windows 11

Desde Windows, `127.0.0.1` identifica Windows, no el equipo Linux. Mantenga SQL enlazado a localhost y use un túnel SSH si ya tiene un servicio SSH autorizado en Linux:

```powershell
ssh -N -L 127.0.0.1:11433:127.0.0.1:1433 usuario_linux@IP_PRIVADA_LINUX
```

En SSMS: servidor `tcp:127.0.0.1,11433`, autenticación SQL Server, `sa`, secreto de `.env`, cifrado obligatorio y confiar en el certificado del servidor para esta conexión local. Mantenga abierta la sesión SSH. No cambie el puerto de Compose a `0.0.0.0`, no abra 1433 a Internet y no active SSH automáticamente: su instalación/configuración es una decisión del administrador.

## Administración y diagnóstico

| Comando | Efecto |
|---|---|
| `make check` | Requisitos y reporte, sin instalar paquetes |
| `make up` | Iniciar/reutilizar motor y volumen, comprobar consulta SQL |
| `make status` | Estado de servicios y healthcheck |
| `make inspect` | Inspeccionar todos los conjuntos de todos los .bak |
| `make verify` | Verificación preliminar por FILE, no restaura |
| `make restore` | Restauración explícita de destinos nuevos |
| `make validate` | Catálogos y CHECKDB completo |
| `make report` | Generar informe sin conectar a Docker |
| `make logs` | Últimas 150 líneas del motor; no imprime secretos conocidos |
| `make down` | Detener/eliminar contenedores y red, **conservar volumen** |
| `make config` | Validación silenciosa del YAML |
| `make test` | Pruebas de barreras de seguridad sin contenedores |

```bash
make down
make up             # reutiliza los datos; no vuelve a restaurar
make status
```

No use `docker compose down -v`, `docker volume rm` ni `docker system prune --volumes` sobre este entorno. La imagen solicitada `2022-latest` cambia con las actualizaciones; el informe registra ID/digest de la descargada. Conserve ese digest y la imagen si necesita reproducir exactamente esta versión. `make up` no fuerza descargar una imagen distinta si ya está disponible.

Problemas frecuentes:

- **Permiso denegado al socket:** `docker info` y `systemctl is-active docker`; el administrador debe revisar servicio y permisos. Los scripts no elevan privilegios automáticamente.
- **Puerto ocupado:** `ss -ltn '( sport = :1433 )'`; identifique al propietario y decida manualmente. No se detienen servicios ajenos.
- **Inicio/credenciales:** `make logs`, `make status`, `./scripts/wait-for-sqlserver.sh`. Compruebe `.env` con permisos 600 y la contraseña vigente del volumen. Los logs del motor pueden contener metadatos; no los publique completos.
- **Archivo no legible / error 3201:** revise nombre exacto y ACL del directorio/copia; `docker compose exec -T sqlserver ls -l /var/opt/mssql/backup`.
- **Medios faltantes / 3132:** obtenga todas las familias del mismo respaldo y solicite un plan DBA; no cambie arbitrariamente `FILE`.
- **Certificado / 33111 o TDE:** conserve certificado y clave privada originales; deben importarse de manera controlada antes de una recuperación especializada.
- **Versión / 3169:** revise `SoftwareVersionMajor` y características; no se fuerza downgrade.
- **Base o archivo de destino existente:** cambie explícitamente el destino en el plan; compruebe antes si un intento previo dejó una base `RESTORING` o archivos. No se borran automáticamente.
- **SHA-256 cambió:** termine la copia del archivo, confirme su integridad con el original y repita `make inspect` y `make verify`.
- **Espacio insuficiente:** revise `docker compose exec -T sqlserver df -h /var/opt/mssql`; el tamaño comprimido del .bak no representa el espacio de la base.
- **CHECKDB falló:** la base no queda certificada. Informe los códigos a un DBA; no ejecute reparaciones con pérdida de datos.
- **Interrupción:** revise `make status`, `make report` y los catálogos antes de repetir. Los intentos iniciados quedan registrados y no se interpretan como éxito.

Los scripts devuelven 0 cuando la operación solicitada termina correctamente y un código distinto de cero ante fallos; Make propaga el fallo. La ausencia de respaldos es advertencia en `check/up` y error en `inspect/verify/restore`. No se ejecutan restauraciones como parte de las pruebas del proyecto.

Con el motor iniciado, `make smoke` prueba la conexión cifrada, crea un respaldo **COPY_ONLY de model** en un archivo temporal único dentro del contenedor, comprueba los esquemas HEADERONLY/FILELISTONLY contra SQL Server real, ejecuta VERIFYONLY con MOVE/CHECKSUM y CHECKDB sin reparaciones sobre model. Elimina exclusivamente ese archivo temporal y guarda `reports/prueba-sqlserver.json`. No crea, elimina ni restaura bases y no utiliza respaldos institucionales. El registro de esa copia en el historial de `msdb` puede permanecer.

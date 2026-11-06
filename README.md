# 🌱 Hyma Agroindustrial · Sistema de Control Fiscal y Gastos

Sistema contable y de tesorería automatizado para empresas agrícolas y agroindustriales. Permite la conciliación de cuentas fiscales, captura manual con autocompletado inteligente y extracción masiva de movimientos mediante estados de cuenta bancarios en PDF.

---

## 🚀 Características Principales

1. **Gestión de Cuentas Bancarias:**
   - Registro único de cuentas con **CLABE (18 dígitos)** y **Número de Cuenta**.
   - Cálculo automático del balance contable en tiempo real: `Saldo Inicial + Entradas - Salidas`.

2. **Captura Híbrida de Transacciones:**
   - **Manual:** Formulario dinámico con categorías vinculadas a tipos de entidades (Clientes, Gasolineras, Proveedores de Insumos).
   - **Automatizada (PDF):** Ingesta y parseo inteligente de estados de cuenta con `pdfplumber` y detección de huella digital SHA-256 para evitar duplicar movimientos.

3. **Autocompletado y Creación al Vuelo:**
   - Si se selecciona la categoría *"Pago Gasolina"*, el menú filtra automáticamente las estaciones registradas.
   - Si se selecciona *"Depósito Cliente"*, filtra la cartera de clientes.
   - Permite crear nuevos clientes/proveedores en un modal sin perder el avance de captura.

4. **Reportes y Auditoría:**
   - Filtros combinados por Rango de Fechas, Cuenta Bancaria, Categoría y Cliente/Proveedor.
   - Desglose de ingresos vs. egresos y balance neto del periodo.

---

## 🛠️ Stack Tecnológico

- **Backend:** Python 3.10+ con **FastAPI** y **SQLAlchemy**.
- **Base de Datos:** SQLite (por defecto para desarrollo rápido) o **PostgreSQL** para producción.
- **Extracción de Documentos:** `pdfplumber`, `re` y hashing SHA-256.
- **Frontend:** HTML5, **Tailwind CSS** y **Alpine.js** (sin necesidad de compilar con Node.js).

---

## 📂 Estructura del Repositorio

```text
hyma-agroindustrial/
├── backend/
│   ├── main.py                     # API FastAPI y orquestador de endpoints
│   ├── database.py                 # Conexión SQLAlchemy (SQLite / PostgreSQL)
│   ├── models.py                   # Modelos de base de datos relacionales
│   ├── schemas.py                  # Validaciones Pydantic v2
│   └── parsers/
│       └── bank_pdf_parser.py      # Motor de extracción de estados de cuenta PDF
├── frontend/
│   └── static/
│       └── index.html              # Dashboard SPA interactivo con Alpine.js y Tailwind
├── schema.sql                      # Esquema DDL en SQL puro
├── requirements.txt                # Dependencias de Python
├── Dockerfile                      # Contenedor para producción
├── docker-compose.yml              # Orquestación con PostgreSQL
├── .gitignore
└── README.md
```

---

## 💻 Instrucciones de Instalación y Ejecución Local

### 1. Clonar el repositorio y crear entorno virtual
```bash
git clone https://github.com/tu-usuario/hyma-agroindustrial.git
cd hyma-agroindustrial

python -m venv venv
# En Windows:
venv\Scripts\activate
# En Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Ejecutar la Aplicación
```bash
uvicorn backend.main:app --reload --port 8000
```

Abre tu navegador en:
- **Panel Web:** `http://localhost:8000`
- **Documentación Swagger API:** `http://localhost:8000/docs`

---

## 🐳 Ejecución con Docker (PostgreSQL)

```bash
docker-compose up --build -d
```
La aplicación creará automáticamente la base de datos PostgreSQL, las tablas y las categorías iniciales.

---

## 📤 Cómo Subir este Proyecto a GitHub

1. Inicializa el repositorio local en la raíz del proyecto:
   ```bash
   git init
   git add .
   git commit -m "feat: arquitectura base de control fiscal y extractor PDF para Hyma Agroindustrial"
   ```

2. Vincula tu repositorio remoto y sube los cambios:
   ```bash
   git branch -M main
   git remote add origin https://github.com/tu-usuario/hyma-agroindustrial.git
   git push -u origin main
   ```


-- ====================================================================================================
-- HYMA AGROINDUSTRIAL - CONTROL DE GASTOS & CUENTAS FISCALES
-- Motor de Base de Datos: Compatible con PostgreSQL (12+) y SQLite (3.30+)
-- ====================================================================================================

-- 1. TABLA DE CUENTAS BANCARIAS
CREATE TABLE IF NOT EXISTS cuentas (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- Para PostgreSQL cambiar por: id SERIAL PRIMARY KEY,
    nombre VARCHAR(100) NOT NULL,
    banco VARCHAR(80) NOT NULL,
    clabe VARCHAR(18) NOT NULL UNIQUE,
    numero_cuenta VARCHAR(25) NOT NULL UNIQUE,
    saldo_inicial NUMERIC(15, 2) NOT NULL DEFAULT 0.00,
    moneda VARCHAR(5) NOT NULL DEFAULT 'MXN',
    activo BOOLEAN NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. TABLA DE CATEGORÍAS
-- Define si la categoría es de entrada, salida y qué tipo de entidad vincula por defecto
CREATE TABLE IF NOT EXISTS categorias (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- Para PostgreSQL: id SERIAL PRIMARY KEY,
    nombre VARCHAR(100) NOT NULL UNIQUE,
    tipo VARCHAR(10) NOT NULL CHECK (tipo IN ('ENTRADA', 'SALIDA', 'AMBOS')),
    tipo_entidad_sugerida VARCHAR(30) NOT NULL DEFAULT 'GENERAL', 
    -- 'CLIENTE', 'GASOLINERA', 'PROVEEDOR', 'EMPLEADO', 'IMPUESTO', 'GENERAL'
    descripcion TEXT,
    activo BOOLEAN NOT NULL DEFAULT 1
);

-- 3. TABLA DE ENTIDADES (CLIENTES, PROVEEDORES, GASOLINERAS, TRABAJADORES)
CREATE TABLE IF NOT EXISTS entidades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- Para PostgreSQL: id SERIAL PRIMARY KEY,
    nombre VARCHAR(150) NOT NULL,
    tipo_entidad VARCHAR(30) NOT NULL, -- 'CLIENTE', 'GASOLINERA', 'PROVEEDOR', 'EMPLEADO', 'SAT'
    rfc VARCHAR(13),
    telefono VARCHAR(20),
    email VARCHAR(100),
    activo BOOLEAN NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_entidad_tipo UNIQUE (nombre, tipo_entidad)
);

-- 4. TABLA DE TRANSACCIONES (MOVIMIENTOS FISCALES)
CREATE TABLE IF NOT EXISTS transacciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- Para PostgreSQL: id SERIAL PRIMARY KEY,
    cuenta_id INTEGER NOT NULL,
    categoria_id INTEGER,
    entidad_id INTEGER,
    fecha DATE NOT NULL,
    tipo VARCHAR(10) NOT NULL CHECK (tipo IN ('ENTRADA', 'SALIDA')),
    monto NUMERIC(15, 2) NOT NULL CHECK (monto > 0),
    concepto TEXT NOT NULL,
    referencia VARCHAR(100),
    origen VARCHAR(15) NOT NULL DEFAULT 'MANUAL' CHECK (origen IN ('MANUAL', 'PDF_EXTRACT')),
    archivo_fuente VARCHAR(255),
    hash_deduplicacion VARCHAR(64) UNIQUE, -- Previene duplicar movimientos si se procesa el PDF dos veces
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cuenta_id) REFERENCES cuentas(id) ON DELETE CASCADE,
    FOREIGN KEY (categoria_id) REFERENCES categorias(id) ON DELETE SET NULL,
    FOREIGN KEY (entidad_id) REFERENCES entidades(id) ON DELETE SET NULL
);

-- ÍNDICES PARA ALTO RENDIMIENTO EN REPORTES
CREATE INDEX IF NOT EXISTS idx_trans_fecha ON transacciones(fecha);
CREATE INDEX IF NOT EXISTS idx_trans_cuenta ON transacciones(cuenta_id);
CREATE INDEX IF NOT EXISTS idx_trans_categoria ON transacciones(categoria_id);
CREATE INDEX IF NOT EXISTS idx_trans_entidad ON transacciones(entidad_id);
CREATE INDEX IF NOT EXISTS idx_entidades_tipo ON entidades(tipo_entidad);

-- SEED DATA BÁSICO (CATEGORÍAS INICIALES PARA EL AGRO Y FISCAL)
INSERT OR IGNORE INTO categorias (nombre, tipo, tipo_entidad_sugerida, descripcion) VALUES
('Depósito Cliente', 'ENTRADA', 'CLIENTE', 'Ventas y cobranza de cosechas/productos agroindustriales'),
('Pago Gasolina', 'SALIDA', 'GASOLINERA', 'Combustible de maquinaria, tractores y transporte de carga'),
('Pago Proveedor Insumos', 'SALIDA', 'PROVEEDOR', 'Fertilizantes, semillas, agroquímicos y refacciones'),
('Pago Nómina / Jornales', 'SALIDA', 'EMPLEADO', 'Dispersión de sueldos o pago de jornales de campo'),
('Impuestos / Retenciones', 'SALIDA', 'SAT', 'Pago de provisionales ISR, IVA, cuotas IMSS'),
('Traspaso Entre Cuentas', 'AMBOS', 'GENERAL', 'Movimientos internos entre cuentas de la misma razón social');


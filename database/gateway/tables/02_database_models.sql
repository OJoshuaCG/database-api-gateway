-- Tabla: database_models
-- Generado por database/generate_schema.py desde los modelos ORM.
-- NO editar a mano: Alembic es la fuente de verdad del esquema.

CREATE TABLE database_models (
	id INTEGER NOT NULL COMMENT 'ID único del blueprint' AUTO_INCREMENT, 
	name VARCHAR(100) NOT NULL COMMENT 'Nombre legible del blueprint (p. ej. ''Whatsapp'')', 
	slug VARCHAR(120) NOT NULL COMMENT 'Identificador estable en kebab/snake-case', 
	description TEXT COMMENT 'Descripción del blueprint', 
	current_version VARCHAR(50) NOT NULL COMMENT 'Versión actual del blueprint (string libre por ahora)' DEFAULT '0.0.0', 
	is_active BOOL NOT NULL COMMENT 'Soft-disable del blueprint' DEFAULT '1', 
	created_at DATETIME NOT NULL COMMENT 'Fecha y hora de creación del registro' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT 'Fecha y hora de última actualización del registro' DEFAULT now(), 
	CONSTRAINT pk_database_models PRIMARY KEY (id)
)COMMENT='Blueprints/categorías de base de datos (plantillas lógicas)';
CREATE UNIQUE INDEX ix_database_models_name ON database_models (name);
CREATE UNIQUE INDEX ix_database_models_slug ON database_models (slug);

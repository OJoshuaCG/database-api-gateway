-- Tabla: privileges
-- Generado por database/generate_schema.py desde los modelos ORM.
-- NO editar a mano: Alembic es la fuente de verdad del esquema.

CREATE TABLE privileges (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	engine VARCHAR(16) NOT NULL COMMENT 'Motor que admite el privilegio: mysql | mariadb | postgresql', 
	name VARCHAR(64) NOT NULL COMMENT 'Token del privilegio, p. ej. SELECT, CREATE VIEW, GRANT OPTION', 
	category VARCHAR(16) NOT NULL COMMENT 'object = otorgable sobre objetos; admin = global/servidor' DEFAULT 'object', 
	context VARCHAR(128) COMMENT 'Niveles donde aplica (informativo), p. ej. ''Tables,Columns''', 
	description VARCHAR(255) NOT NULL COMMENT 'Qué permite el privilegio (breve)', 
	is_sensitive BOOL NOT NULL COMMENT 'Requiere confirmación extra al otorgar (ALL, GRANT OPTION, MAINTAIN)' DEFAULT '0', 
	is_active BOOL NOT NULL COMMENT 'Si la plataforma controla/expone este privilegio' DEFAULT '1', 
	created_at DATETIME NOT NULL COMMENT 'Fecha y hora de creación del registro' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT 'Fecha y hora de última actualización del registro' DEFAULT now(), 
	CONSTRAINT pk_privileges PRIMARY KEY (id), 
	CONSTRAINT uq_privileges_engine_name UNIQUE (engine, name)
)COMMENT='Catálogo de privilegios soportados por cada motor de BD';
CREATE INDEX ix_privileges_engine ON privileges (engine);

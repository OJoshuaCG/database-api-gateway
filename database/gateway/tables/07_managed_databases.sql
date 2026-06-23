-- Tabla: managed_databases
-- Generado por database/generate_schema.py desde los modelos ORM.
-- NO editar a mano: Alembic es la fuente de verdad del esquema.

CREATE TABLE managed_databases (
	id INTEGER NOT NULL COMMENT 'ID único de la BD gestionada' AUTO_INCREMENT, 
	name VARCHAR(64) NOT NULL COMMENT 'Nombre de la base de datos en el motor', 
	server_id INTEGER NOT NULL COMMENT 'Servidor donde vive la base de datos', 
	owner_id INTEGER NOT NULL COMMENT 'Usuario del motor propietario (único). RESTRICT: reasignar antes de borrar', 
	model_id INTEGER COMMENT 'Blueprint que replica esta BD (opcional)', 
	model_version VARCHAR(50) COMMENT 'Versión del blueprint implementada', 
	charset VARCHAR(64) COMMENT 'Charset (MySQL/MariaDB); p. ej. utf8mb4', 
	collation VARCHAR(64) COMMENT 'Collation (MySQL/MariaDB)', 
	status VARCHAR(20) NOT NULL COMMENT 'Estado de consistencia inventario↔motor' DEFAULT 'pending', 
	notes TEXT COMMENT 'Notas / detalle de error de aprovisionamiento', 
	created_at DATETIME NOT NULL COMMENT 'Fecha y hora de creación del registro' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT 'Fecha y hora de última actualización del registro' DEFAULT now(), 
	CONSTRAINT pk_managed_databases PRIMARY KEY (id), 
	CONSTRAINT uq_managed_databases_server_name UNIQUE (server_id, name), 
	CONSTRAINT fk_managed_databases_server_id_servers FOREIGN KEY(server_id) REFERENCES servers (id) ON DELETE CASCADE, 
	CONSTRAINT fk_managed_databases_owner_id_server_users FOREIGN KEY(owner_id) REFERENCES server_users (id) ON DELETE RESTRICT, 
	CONSTRAINT fk_managed_databases_model_id_database_models FOREIGN KEY(model_id) REFERENCES database_models (id) ON DELETE SET NULL
)COMMENT='Bases de datos reales gestionadas por el gateway en cada servidor';
CREATE INDEX ix_managed_databases_model_id ON managed_databases (model_id);
CREATE INDEX ix_managed_databases_owner_id ON managed_databases (owner_id);
CREATE INDEX ix_managed_databases_server_id ON managed_databases (server_id);

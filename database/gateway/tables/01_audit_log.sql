-- Tabla: audit_log
-- Generado por database/generate_schema.py desde los modelos ORM.
-- NO editar a mano: Alembic es la fuente de verdad del esquema.

CREATE TABLE audit_log (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	request_id VARCHAR(32) COMMENT 'Request ID de la operación', 
	admin_id INTEGER COMMENT 'ID del admin que ejecutó la acción', 
	admin_username VARCHAR(128), 
	action VARCHAR(64) NOT NULL COMMENT 'Acción, p. ej. ''managed_database.create''', 
	target_type VARCHAR(64), 
	target_id INTEGER, 
	server_id INTEGER, 
	touched_engine BOOL NOT NULL COMMENT 'True si la operación ejecutó DDL/DCL en un motor destino' DEFAULT '0', 
	status VARCHAR(20) NOT NULL COMMENT 'success | error', 
	detail TEXT COMMENT 'Resumen corto SIN credenciales', 
	ip VARCHAR(64), 
	created_at DATETIME NOT NULL COMMENT 'Fecha y hora de creación del registro' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT 'Fecha y hora de última actualización del registro' DEFAULT now(), 
	CONSTRAINT pk_audit_log PRIMARY KEY (id)
)COMMENT='Auditoría de operaciones sensibles del gateway';
CREATE INDEX ix_audit_log_action ON audit_log (action);
CREATE INDEX ix_audit_log_request_id ON audit_log (request_id);
CREATE INDEX ix_audit_log_server_id ON audit_log (server_id);

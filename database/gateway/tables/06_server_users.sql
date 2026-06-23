-- Tabla: server_users
-- Generado por database/generate_schema.py desde los modelos ORM.
-- NO editar a mano: Alembic es la fuente de verdad del esquema.

CREATE TABLE server_users (
	id INTEGER NOT NULL COMMENT 'ID único del usuario' AUTO_INCREMENT, 
	server_id INTEGER NOT NULL COMMENT 'Servidor destino al que pertenece el usuario', 
	username VARCHAR(128) NOT NULL COMMENT 'Nombre del usuario/rol en el motor', 
	host VARCHAR(255) NOT NULL COMMENT 'Host MySQL (''user''@''host''); ignorado en PostgreSQL' DEFAULT '%%', 
	password_encrypted TEXT COMMENT 'Password del usuario CIFRADO (Fernet), opcional. Nunca se expone', 
	is_active BOOL NOT NULL COMMENT 'Soft-disable del usuario en el inventario' DEFAULT '1', 
	notes TEXT COMMENT 'Notas adicionales sobre el usuario', 
	created_at DATETIME NOT NULL COMMENT 'Fecha y hora de creación del registro' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT 'Fecha y hora de última actualización del registro' DEFAULT now(), 
	CONSTRAINT pk_server_users PRIMARY KEY (id), 
	CONSTRAINT uq_server_users_server_username_host UNIQUE (server_id, username, host), 
	CONSTRAINT fk_server_users_server_id_servers FOREIGN KEY(server_id) REFERENCES servers (id) ON DELETE CASCADE
)COMMENT='Usuarios del motor (propietarios) gestionados por el gateway';
CREATE INDEX ix_server_users_server_id ON server_users (server_id);

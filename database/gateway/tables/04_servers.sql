-- Tabla: servers
-- Generado por database/generate_schema.py desde los modelos ORM.
-- NO editar a mano: Alembic es la fuente de verdad del esquema.

CREATE TABLE servers (
	id INTEGER NOT NULL COMMENT 'ID único del servidor' AUTO_INCREMENT, 
	name VARCHAR(100) NOT NULL COMMENT 'Alias legible del servidor', 
	host VARCHAR(255) NOT NULL COMMENT 'Hostname o IP del servidor destino', 
	port INTEGER NOT NULL COMMENT 'Puerto de conexión del motor', 
	engine VARCHAR(20) NOT NULL COMMENT 'Motor de base de datos: mysql | mariadb | postgresql', 
	root_username VARCHAR(128) NOT NULL COMMENT 'Usuario pseudo-root para administrar el servidor', 
	root_password_encrypted TEXT NOT NULL COMMENT 'Password pseudo-root CIFRADO (Fernet). Nunca se expone ni se loguea', 
	status VARCHAR(20) NOT NULL COMMENT 'Estado operativo del servidor en el inventario' DEFAULT 'active', 
	is_active BOOL NOT NULL COMMENT 'Permite deshabilitar el servidor sin borrarlo (soft-disable)' DEFAULT '1', 
	notes TEXT COMMENT 'Notas adicionales sobre el servidor', 
	created_at DATETIME NOT NULL COMMENT 'Fecha y hora de creación del registro' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT 'Fecha y hora de última actualización del registro' DEFAULT now(), 
	CONSTRAINT pk_servers PRIMARY KEY (id), 
	CONSTRAINT uq_servers_host_port UNIQUE (host, port)
)COMMENT='Servidores de base de datos destino administrados por el gateway';
CREATE UNIQUE INDEX ix_servers_name ON servers (name);

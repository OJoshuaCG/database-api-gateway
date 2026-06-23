-- Esquema completo de la BD de metadatos del gateway
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

CREATE TABLE users (
	id INTEGER NOT NULL COMMENT 'ID único del usuario' AUTO_INCREMENT, 
	username VARCHAR(50) NOT NULL COMMENT 'Nombre de usuario único para login', 
	email VARCHAR(255) NOT NULL COMMENT 'Correo electrónico único del usuario', 
	hashed_password VARCHAR(255) NOT NULL COMMENT 'Contraseña hasheada (bcrypt/argon2)', 
	full_name VARCHAR(100) COMMENT 'Nombre completo del usuario', 
	notes TEXT COMMENT 'Notas adicionales sobre el usuario', 
	is_active BOOL NOT NULL COMMENT 'Indica si el usuario está activo en el sistema' DEFAULT '1', 
	is_superuser BOOL NOT NULL COMMENT 'Indica si el usuario tiene privilegios de superusuario' DEFAULT '0', 
	created_at DATETIME NOT NULL COMMENT 'Fecha y hora de creación del registro' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT 'Fecha y hora de última actualización del registro' DEFAULT now(), 
	CONSTRAINT pk_users PRIMARY KEY (id)
)COMMENT='Tabla de usuarios del sistema';

CREATE UNIQUE INDEX ix_users_email ON users (email);
CREATE UNIQUE INDEX ix_users_username ON users (username);

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


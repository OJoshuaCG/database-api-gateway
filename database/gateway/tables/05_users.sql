-- Tabla: users
-- Generado por database/generate_schema.py desde los modelos ORM.
-- NO editar a mano: Alembic es la fuente de verdad del esquema.

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

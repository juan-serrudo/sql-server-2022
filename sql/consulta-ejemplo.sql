-- Consulta técnica, sin leer datos de tablas de usuario.
SET NOCOUNT ON;
SELECT name, state_desc, compatibility_level
FROM sys.databases
WHERE name IN (N'fiscalia2015', N'fiscalia2016', N'fiscalia2017');

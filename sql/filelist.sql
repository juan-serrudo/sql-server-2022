CREATE TABLE #f (
 LogicalName nvarchar(128), PhysicalName nvarchar(260), Type char(1),
 FileGroupName nvarchar(128), Size numeric(20,0), MaxSize numeric(20,0),
 FileID bigint, CreateLSN numeric(25,0), DropLSN numeric(25,0), UniqueID uniqueidentifier,
 ReadOnlyLSN numeric(25,0), ReadWriteLSN numeric(25,0), BackupSizeInBytes bigint,
 SourceBlockSize int, FileGroupID int, LogGroupGUID uniqueidentifier,
 DifferentialBaseLSN numeric(25,0), DifferentialBaseGUID uniqueidentifier,
 IsReadOnly bit, IsPresent bit, TDEThumbprint varbinary(32), SnapshotURL nvarchar(360)
);
INSERT INTO #f EXEC(N'RESTORE FILELISTONLY FROM DISK = @@DISK_EXEC@@ WITH FILE = @@POSITION@@;');
SELECT N'__JSON_BEGIN__';
SELECT LogicalName, PhysicalName, Type, Size, FileID, BackupSizeInBytes,
 IsPresent, TDEThumbprint, SnapshotURL
FROM #f ORDER BY FileID FOR JSON PATH, INCLUDE_NULL_VALUES;
SELECT N'__JSON_END__';

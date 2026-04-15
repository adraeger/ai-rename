-- DEVONthink Smart Rule: ruft ai-rename.py auf einer Kopie der Datei auf,
-- liest den neuen Dateinamen aus und setzt ihn als DEVONthink-Name.
-- So bleibt das Original unberuehrt und DEVONthink uebernimmt die eigentliche
-- Umbenennung (funktioniert sowohl fuer indizierte als auch importierte Items).

on performSmartRule(theRecords)
	set pythonPath to (POSIX path of (path to home folder)) & ".local/bin/ai-rename.py"
	tell application id "DNtp"
		repeat with theRecord in theRecords
			try
				set oldPath to path of theRecord
				if oldPath is missing value or oldPath is "" then error "Kein Dateipfad"

				set origFilename to filename of theRecord
				if origFilename is missing value or origFilename is "" then set origFilename to "document.pdf"

				-- Kopie in Temp-Verzeichnis: Python benennt dort um, Original bleibt unberuehrt
				set tmpDir to do shell script "mktemp -d -t airename"
				set tmpFile to tmpDir & "/" & origFilename
				do shell script "/bin/cp " & quoted form of oldPath & " " & quoted form of tmpFile

				try
					do shell script "/usr/bin/python3 " & quoted form of pythonPath & " " & quoted form of tmpFile
				on error pyErr
					do shell script "/bin/rm -rf " & quoted form of tmpDir
					error "ai-rename: " & pyErr
				end try

				-- Neuer Dateiname = einzige Datei im Temp-Verzeichnis
				set newFilename to do shell script "ls -1A " & quoted form of tmpDir & " | head -n 1"
				do shell script "/bin/rm -rf " & quoted form of tmpDir

				if newFilename is "" then error "Kein neuer Dateiname erkannt"
				if newFilename is origFilename then error "Keine Umbenennung (Datum/Titel nicht erkannt)"

				-- Extension entfernen (DEVONthink haengt sie bei 'name' automatisch an)
				if newFilename ends with ".pdf" then
					set newName to text 1 thru -5 of newFilename
				else
					set newName to newFilename
				end if

				set name of theRecord to newName
			on error errMsg
				log message "AI Rename Fehler: " & errMsg
			end try
		end repeat
	end tell
end performSmartRule

-- Optional: Testlauf aus dem Script Editor mit aktuell ausgewaehlten Records
on run
	tell application id "DNtp"
		set sel to selection
	end tell
	if sel is {} then
		display dialog "Bitte in DEVONthink ein oder mehrere PDFs auswaehlen." buttons {"OK"} default button 1
		return
	end if
	performSmartRule(sel)
end run

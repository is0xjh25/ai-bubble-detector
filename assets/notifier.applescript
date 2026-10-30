-- Bubble Detector notifier. Applets do not receive argv when run directly, so the caller sets
-- environment variables instead:  BD_TITLE, BD_SUBTITLE, BD_MESSAGE, BD_SOUND ("silent" to mute).
on envOr(name, fallback)
	try
		return system attribute name
	on error
		return fallback
	end try
end envOr

set titleText to envOr("BD_TITLE", "Bubble Detector")
set subtitleText to envOr("BD_SUBTITLE", "")
set messageText to envOr("BD_MESSAGE", "")
set soundFlag to envOr("BD_SOUND", "")
if soundFlag is "silent" then
	display notification messageText with title titleText subtitle subtitleText
else
	display notification messageText with title titleText subtitle subtitleText sound name "default"
end if
delay 1 -- let Notification Center take the request before the process exits

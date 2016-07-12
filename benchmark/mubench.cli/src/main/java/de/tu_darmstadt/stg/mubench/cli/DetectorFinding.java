package de.tu_darmstadt.stg.mubench.cli;

import java.util.Collection;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;

public class DetectorFinding {
	private static final String keyFile = "file";
	private static final String keyMethod = "method";

	private final HashMap<String, Object> content;

	public DetectorFinding(String file, String method) {
		content = new LinkedHashMap<>();
		if (file != null) {
			content.put(keyFile, file);
		}
		if (method != null) {
			content.put(keyMethod, method);
		}
	}

	public String put(String key, String value) {
		return (String) content.put(key, value);
	}
	
	public Collection<String> put(String key, Collection<String> values) {
		return (Collection<String>) content.put(key, values);
	}

	public Map<String, Object> getContent() {
		return content;
	}
}

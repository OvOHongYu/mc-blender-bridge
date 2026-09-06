package com.zcube.mcbridge.http;

import java.io.UnsupportedEncodingException;
import java.net.URLDecoder;
import java.util.HashMap;
import java.util.Map;

/** 查询串解析。 */
public final class Query {
    private final Map<String, String> kv = new HashMap<>();

    public Query(String raw) {
        if (raw == null || raw.isEmpty()) {
            return;
        }
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String k = eq < 0 ? pair : pair.substring(0, eq);
            String v = eq < 0 ? "" : pair.substring(eq + 1);
            try {
                kv.put(URLDecoder.decode(k, "UTF-8"), URLDecoder.decode(v, "UTF-8"));
            } catch (UnsupportedEncodingException e) {
                kv.put(k, v);
            }
        }
    }

    public String get(String key, String def) {
        return kv.getOrDefault(key, def);
    }

    public int getInt(String key) {
        return Integer.parseInt(kv.get(key));
    }

    public int getInt(String key, int def) {
        String v = kv.get(key);
        return v == null ? def : Integer.parseInt(v);
    }

    public double getDouble(String key, double def) {
        String v = kv.get(key);
        return v == null ? def : Double.parseDouble(v);
    }
}

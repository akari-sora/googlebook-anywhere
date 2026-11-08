package org.googlebook.noota;
import android.os.Binder;
import android.os.IBinder;
import android.os.Parcel;
import android.os.RemoteException;
import java.lang.reflect.Method;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/** QEMU-only raw-protocol negative probe: never names an external file or block device. */
public final class Probe {
    static final String DESCRIPTOR="android.os.IUpdateEngine";
    static final class Callback extends Binder {
        volatile int status=-1; volatile float progress=-1; final CountDownLatch received=new CountDownLatch(1);
        Callback() { attachInterface(null, "android.os.IUpdateEngineCallback"); }
        @Override protected boolean onTransact(int code, Parcel data, Parcel reply, int flags)
                throws RemoteException {
            if(code!=1) return super.onTransact(code, data, reply, flags);
            data.enforceInterface("android.os.IUpdateEngineCallback");
            status=data.readInt(); progress=data.readFloat(); received.countDown(); return true;
        }
    }
    static void arguments(int code, Parcel data, IBinder cb) {
        switch (code) {
            case 1: data.writeString("file:///nonexistent/gbos-test-payload"); data.writeLong(0); data.writeLong(0); data.writeStringArray(new String[0]); break;
            case 2: data.writeInt(0); data.writeLong(0); data.writeLong(0); data.writeStringArray(new String[0]); break;
            case 3: case 4: case 13: data.writeStrongBinder(cb); break;
            case 9: case 11: case 14: data.writeString("/nonexistent/gbos-test-payload"); break;
            case 12: data.writeString("/nonexistent/gbos-test-payload"); data.writeStringArray(new String[0]); break;
            default: break;
        }
    }
    public static void main(String[] args) throws Exception {
        try {
            if(args.length==1 && args[0].equals("--hold-callbacks")) { holdCallbacks(); return; }
            run(args); lifecycle(); android.util.Log.e("GBOS_QEMU_PROBE", "PROBE_PASS");
        }
        catch (Exception | Error e) { android.util.Log.e("GBOS_QEMU_PROBE", "PROBE_FAIL", e); throw e; }
    }
    static void run(String[] args) throws Exception {
        if(args.length!=0)throw new IllegalArgumentException("no arguments accepted");
        Class<?> sm=Class.forName("android.os.ServiceManager");
        Method check=sm.getMethod("checkService",String.class);
        IBinder service=(IBinder)check.invoke(null,"android.os.UpdateEngineService");
        if(service==null)throw new IllegalStateException("missing service");
        Callback cb=new Callback();
        for(int code=1;code<=14;code++) {
            Parcel d=Parcel.obtain(),r=Parcel.obtain();
            try {
                d.writeInterfaceToken(DESCRIPTOR);arguments(code,d,cb);
                if(!service.transact(code,d,r,0))throw new IllegalStateException("unsupported transaction "+code);
                try {
                    r.readException();
                    if(code==3||code==4){if(!r.readBoolean())throw new IllegalStateException("bind/unbind failed");}
                    else if(code==11){if(r.readBoolean())throw new IllegalStateException("unexpected verify success");}
                    else throw new IllegalStateException("write transaction accepted "+code);
                } catch(SecurityException e) {
                    if(code==3||code==4||code==11)throw e;
                }
                if(code==3 && (!cb.received.await(5,TimeUnit.SECONDS)||cb.status!=0||cb.progress!=0f)) throw new IllegalStateException("idle callback missing or malformed");
                android.util.Log.e("GBOS_QEMU_PROBE", "PROBE_TX code="+code+" result="+((code==3||code==4)?"bound":code==11?"unsupported":"denied"));
            } finally { d.recycle(); r.recycle(); }
        }
    }
    static IBinder service() throws Exception {
        Class<?> sm=Class.forName("android.os.ServiceManager");
        IBinder svc=(IBinder)sm.getMethod("checkService",String.class).invoke(null,"android.os.UpdateEngineService");
        if(svc==null) throw new IllegalStateException("missing service");
        return svc;
    }
    static boolean callbackOp(IBinder service, int code, Callback cb) throws Exception {
        Parcel d=Parcel.obtain(), r=Parcel.obtain();
        try {
            d.writeInterfaceToken(DESCRIPTOR); d.writeStrongBinder(cb);
            if(!service.transact(code,d,r,0)) throw new IllegalStateException("unsupported callback op");
            r.readException(); return r.readBoolean();
        } finally { d.recycle(); r.recycle(); }
    }
    static void idle(Callback cb) throws Exception {
        if(!cb.received.await(5,TimeUnit.SECONDS)||cb.status!=0||cb.progress!=0f)
            throw new IllegalStateException("idle callback missing or malformed");
    }
    static void holdCallbacks() throws Exception {
        IBinder svc=service(); List<Callback> callbacks=new ArrayList<>();
        for(int i=0;i<64;i++) {
            Callback cb=new Callback(); if(!callbackOp(svc,3,cb)) throw new IllegalStateException("child bind "+i);
            callbacks.add(cb); idle(cb);
        }
        System.out.println("CHILD_READY_64"); System.out.flush();
        if(System.in.read()!=-1) throw new IllegalStateException("unexpected child input");
    }
    static void lifecycle() throws Exception {
        IBinder svc=service();
        Process child=new ProcessBuilder("/system/bin/app_process64","/system/bin",Probe.class.getName(),"--hold-callbacks").redirectErrorStream(true).start();
        ExecutorService reader=Executors.newSingleThreadExecutor();
        try {
            Future<String> ready=reader.submit(() -> {
                BufferedReader input=new BufferedReader(new InputStreamReader(child.getInputStream()));
                for(String line;(line=input.readLine())!=null;) if(line.equals("CHILD_READY_64")) return line;
                return "EOF";
            });
            if(!"CHILD_READY_64".equals(ready.get(45,TimeUnit.SECONDS))||!child.isAlive()) throw new IllegalStateException("child not ready");
            Callback overflow=new Callback();
            if(callbackOp(svc,3,overflow)) { callbackOp(svc,4,overflow); throw new IllegalStateException("overflow accepted"); }
            if(!child.isAlive()) throw new IllegalStateException("child exited during overflow check");
            android.util.Log.e("GBOS_QEMU_PROBE","LIFECYCLE_CHILD_READY_64_OVERFLOW_REJECTED");
            child.destroyForcibly();
            if(!child.waitFor(10,TimeUnit.SECONDS)||child.isAlive()) throw new IllegalStateException("child did not exit");
            android.util.Log.e("GBOS_QEMU_PROBE","LIFECYCLE_CHILD_EXITED");
            List<Callback> replacements=new ArrayList<>(); long deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(20);
            try {
                while(replacements.size()<64 && System.nanoTime()<deadline) {
                    Callback cb=new Callback();
                    if(callbackOp(svc,3,cb)) { replacements.add(cb); idle(cb); }
                    else Thread.sleep(100);
                }
                if(replacements.size()!=64) throw new IllegalStateException("capacity not reclaimed "+replacements.size());
                android.util.Log.e("GBOS_QEMU_PROBE","LIFECYCLE_RECLAIMED_64_IDLE");
            } finally {
                for(Callback cb:replacements) if(!callbackOp(svc,4,cb)) throw new IllegalStateException("replacement unbind");
            }
            android.util.Log.e("GBOS_QEMU_PROBE","LIFECYCLE_REPLACEMENTS_UNBOUND_64");
            stress(svc);
        } finally {
            if(child.isAlive()) { child.destroyForcibly(); child.waitFor(10,TimeUnit.SECONDS); }
            reader.shutdownNow();
        }
    }
    static void stress(IBinder svc) throws Exception {
        ExecutorService pool=Executors.newFixedThreadPool(8); CountDownLatch start=new CountDownLatch(1);
        List<Future<?>> futures=new ArrayList<>();
        try {
            for(int t=0;t<8;t++) futures.add(pool.submit(() -> {
                start.await();
                for(int i=0;i<4;i++) {
                    Callback cb=new Callback();
                    if(!callbackOp(svc,3,cb)) throw new IllegalStateException("stress bind");
                    try { idle(cb); } finally { if(!callbackOp(svc,4,cb)) throw new IllegalStateException("stress unbind"); }
                }
                return null;
            }));
            start.countDown();
            long deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(30);
            for(Future<?> f:futures) { long remaining=deadline-System.nanoTime(); if(remaining<=0) throw new IllegalStateException("stress timeout"); f.get(remaining,TimeUnit.NANOSECONDS); }
        } finally { pool.shutdownNow(); }
        android.util.Log.e("GBOS_QEMU_PROBE","CONCURRENT_DISTINCT_BIND_UNBIND_32");
    }

}

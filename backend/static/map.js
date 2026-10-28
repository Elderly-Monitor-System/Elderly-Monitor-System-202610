/* 地图：WGS84 输入、GCJ-02 显示、北京时间 */

/* 基础配置 */

let DEVICE_ID=new URLSearchParams(location.search).get("device_id") || "";   // 留空时使用最近上报设备

const RELOCATE_DISTANCE=5000;
const REALTIME_REFRESH_INTERVAL=2000;

// 无定位时的默认地图中心
const DEFAULT_MAP_CENTER=[116.397428,39.90923];

let AMap=null;
let map=null;
let marker=null;

// 接口经纬度使用 WGS84
let latestLongitude=null;
let latestLatitude=null;
let lastLongitude=null;
let lastLatitude=null;

let historyPolyline=null;
let historyStartMarker=null;
let historyEndMarker=null;
let historyQueryMarker=null;
let queryInfoWindow=null;

/* 模式：实时、今日、历史 */
let currentMode="realtime";
let currentHistoryDate=null;

/* Marker 样式 */

const realtimeMarkerContent=`
<div style="
    width:42px;height:42px;background:#1677ff;
    border:4px solid white;border-radius:50% 50% 50% 0;
    transform:rotate(-45deg);box-shadow:0 2px 8px rgba(0,0,0,0.35);
    display:flex;align-items:center;justify-content:center;
">
    <div style="width:12px;height:12px;background:white;border-radius:50%;"></div>
</div>
`;

const startMarkerContent=`
<div style="
    width:34px;height:34px;background:#22a06b;border-radius:50%;
    border:3px solid white;color:white;font-weight:bold;
    display:flex;align-items:center;justify-content:center;
    box-shadow:0 2px 6px rgba(0,0,0,0.3);
">起</div>
`;

const endMarkerContent=`
<div style="
    width:34px;height:34px;background:#e5484d;border-radius:50%;
    border:3px solid white;color:white;font-weight:bold;
    display:flex;align-items:center;justify-content:center;
    box-shadow:0 2px 6px rgba(0,0,0,0.3);
">终</div>
`;

const queryMarkerContent=`
<div style="
    width:34px;height:34px;background:#fa8c16;border-radius:50%;
    border:3px solid white;color:white;font-weight:bold;
    display:flex;align-items:center;justify-content:center;
    box-shadow:0 2px 6px rgba(0,0,0,0.3);
">时</div>
`;

/* 通用工具函数 */

let gpsFixEpoch = NaN;
let gpsLastStatus = "正在获取";
function updateGpsStatus(text){
    gpsLastStatus=text;
    const el=document.getElementById("gpsStatus");
    const stale=Number.isFinite(gpsFixEpoch) && Date.now()-gpsFixEpoch>60000;
    if(el){
        el.textContent=text==="正常" ? (stale?"定位已过期":"定位正常") : text;
        el.dataset.tone=text==="正常" ? (stale?"warn":"ok") : /失败|无法/.test(text)?"error":"warn";
    }
}
function updateFreshness(){
    const el=document.getElementById("gpsFreshness");
    if(!Number.isFinite(gpsFixEpoch)){el.textContent="等待有效定位";return;}
    const seconds=Math.max(0,Math.floor((Date.now()-gpsFixEpoch)/1000));
    el.textContent=seconds<60 ? seconds+" 秒前更新" : seconds<3600 ? Math.floor(seconds/60)+" 分钟前更新" : Math.floor(seconds/3600)+" 小时前更新";
    updateGpsStatus(gpsLastStatus);
}
function updateGpsTime(text){
    document.getElementById("gpsTime").textContent="定位时间："+(text || "暂无")+ (text?"（北京时间）":"");
    gpsFixEpoch=text ? new Date(text.replace(" ","T")+"+08:00").getTime() : NaN;
    updateFreshness();
}
function updateModeStatus(text){
    document.getElementById("modeStatus").textContent="当前模式："+text;
    const ids={realtime:"realtimeButton",today:"todayTrackButton",history:"historyButton"};
    Object.keys(ids).forEach(function(mode){
        const button=document.getElementById(ids[mode]);
        button.classList.toggle("active",mode===currentMode);
        button.setAttribute("aria-pressed",String(mode===currentMode));
    });
}
function notifyMap(text){
    const el=document.getElementById("mapFeedback");el.textContent=text;el.hidden=false;
}
function showMapFailure(){
    const el=document.getElementById("mapMessage");
    el.hidden=false;
    el.querySelector("strong").textContent="地图暂时无法加载";
    document.getElementById("mapMessageText").textContent="请检查网络及地图服务配置后重试。已有记录不会因此丢失。";
    document.getElementById("mapRetry").hidden=false;
}

function showHistoryPanel(show){
    const panel=document.getElementById("historyPanel");
    if(panel)panel.style.display=show?"block":"none";
}

function showTodayQueryPanel(show){
    const panel=document.getElementById("todayQueryPanel");
    if(panel)panel.style.display=show?"block":"none";
}

function normalizeTimeString(value){
    if(!value)return "";

    // 兼容 HH:MM 与 HH:MM:SS

    if(/^\d{2}:\d{2}$/.test(value)){
        return value+":00";
    }

    return value;
}

/* 北京时间日期工具 */

function getBeijingDate(offsetDays=0){
    const now=Date.now();
    const shifted=new Date(
        now + 8*60*60*1000 - offsetDays*24*60*60*1000
    );
    return shifted.toISOString().slice(0,10);
}

function getDateValue(offsetDays){
    return getBeijingDate(offsetDays);
}

function getTodayDateString(){
    return getBeijingDate(0);
}

/* 两点距离计算 */

function calculateDistance(lon1,lat1,lon2,lat2){
    const R=6371000;
    const radLat1=lat1*Math.PI/180;
    const radLat2=lat2*Math.PI/180;
    const dLat=(lat2-lat1)*Math.PI/180;
    const dLon=(lon2-lon1)*Math.PI/180;

    const a=Math.sin(dLat/2)**2+
        Math.cos(radLat1)*Math.cos(radLat2)*Math.sin(dLon/2)**2;

    const c=2*Math.atan2(Math.sqrt(a),Math.sqrt(1-a));
    return R*c;
}

/* WGS84 → GCJ-02 */

const GCJ_PI=Math.PI;
const GCJ_A=6378245.0;
const GCJ_EE=0.00669342162296594323;

function outOfChina(lng,lat){
    return lng<72.004 || lng>137.8347 || lat<0.8293 || lat>55.8271;
}

function transformLat(x,y){
    let ret=-100.0+2.0*x+3.0*y+0.2*y*y+0.1*x*y+0.2*Math.sqrt(Math.abs(x));
    ret+=(20.0*Math.sin(6.0*x*GCJ_PI)+20.0*Math.sin(2.0*x*GCJ_PI))*2.0/3.0;
    ret+=(20.0*Math.sin(y*GCJ_PI)+40.0*Math.sin(y/3.0*GCJ_PI))*2.0/3.0;
    ret+=(160.0*Math.sin(y/12.0*GCJ_PI)+320*Math.sin(y*GCJ_PI/30.0))*2.0/3.0;
    return ret;
}

function transformLng(x,y){
    let ret=300.0+x+2.0*y+0.1*x*x+0.1*x*y+0.1*Math.sqrt(Math.abs(x));
    ret+=(20.0*Math.sin(6.0*x*GCJ_PI)+20.0*Math.sin(2.0*x*GCJ_PI))*2.0/3.0;
    ret+=(20.0*Math.sin(x*GCJ_PI)+40.0*Math.sin(x/3.0*GCJ_PI))*2.0/3.0;
    ret+=(150.0*Math.sin(x/12.0*GCJ_PI)+300.0*Math.sin(x/30.0*GCJ_PI))*2.0/3.0;
    return ret;
}

function convertWGS84ToGCJ02(lng,lat){
    lng=Number(lng);
    lat=Number(lat);

    if(!Number.isFinite(lng) || !Number.isFinite(lat)){
        throw new Error("无效经纬度，无法进行坐标转换");
    }

    if(outOfChina(lng,lat)){
        return [lng,lat];
    }

    let dLat=transformLat(lng-105.0,lat-35.0);
    let dLng=transformLng(lng-105.0,lat-35.0);
    const radLat=lat/180.0*GCJ_PI;
    let magic=Math.sin(radLat);
    magic=1-GCJ_EE*magic*magic;
    const sqrtMagic=Math.sqrt(magic);

    dLat=(dLat*180.0)/((GCJ_A*(1-GCJ_EE))/(magic*sqrtMagic)*GCJ_PI);
    dLng=(dLng*180.0)/(GCJ_A/sqrtMagic*Math.cos(radLat)*GCJ_PI);

    return [lng+dLng,lat+dLat];
}

function locationToMapPoint(location){
    return convertWGS84ToGCJ02(
        Number(location.longitude),
        Number(location.latitude)
    );
}

/* API */

function buildApiUrl(path,params={}){
    const search=new URLSearchParams(params);
    if(DEVICE_ID) search.set("device_id",DEVICE_ID);
    return path+"?"+search.toString();
}

async function fetchJson(url,errorMessage){
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),8000);
    let response;
    try{response=await fetch(url,{cache:"no-store",signal:controller.signal});}
    finally{clearTimeout(timeout);}

    let data=null;
    try{
        data=await response.json();
    }catch(error){
        data=null;
    }

    if(!response.ok){
        const detail=data && data.detail ? data.detail : errorMessage;
        throw new Error(detail);
    }

    return data;
}

async function getLatestLocation(){
    return await fetchJson(
        buildApiUrl("/location"),
        "实时定位接口错误"
    );
}

async function getTodayTrack(){
    return await fetchJson(
        buildApiUrl("/locations/today"),
        "今日轨迹接口错误"
    );
}

async function getHistoryByDate(date){
    return await fetchJson(
        buildApiUrl(
            "/locations/history/date",
            {date_str:date}
        ),
        "历史轨迹接口错误"
    );
}

async function getNearestLocation(date,time){
    return await fetchJson(
        buildApiUrl(
            "/locations/nearest",
            {
                date_str:date,
                time_str:normalizeTimeString(time)
            }
        ),
        "时间查询接口错误"
    );
}

/* 历史日期选择 */

function createHistoryDateList(){
    const select=document.getElementById("historyDateSelect");
    select.innerHTML="";

    const placeholder=document.createElement("option");
    placeholder.value="";
    placeholder.textContent="请选择历史日期";
    select.appendChild(placeholder);

    for(let i=1;i<=5;i++){
        const option=document.createElement("option");
        option.value=getDateValue(i);
        option.textContent=(i===1?"昨天 ":i+"天前 ")+option.value;
        select.appendChild(option);
    }

    const customOption=document.createElement("option");
    customOption.value="__custom__";
    customOption.textContent="自定义日期";
    select.appendChild(customOption);

    // 默认日期：昨天
    select.selectedIndex=1;

    const customInput=document.getElementById("customDate");
    customInput.max=getDateValue(1);

    updateCustomDateVisibility();
}

function updateCustomDateVisibility(){
    const select=document.getElementById("historyDateSelect");
    const wrap=document.getElementById("customDateWrap");
    const input=document.getElementById("customDate");

    if(select.value==="__custom__"){
        wrap.style.display="block";
        input.max=getDateValue(1);
        if(!input.value)input.value=getDateValue(1);
    }else{
        wrap.style.display="none";
        input.value="";
    }
}

function getSelectedHistoryDate(){
    const select=document.getElementById("historyDateSelect");

    if(select.value==="__custom__"){
        return document.getElementById("customDate").value;
    }

    return select.value;
}

function updateHistoryTimeVisibility(){
    const mode=document.getElementById("historyTimeMode").value;
    const wrap=document.getElementById("historyTimeWrap");
    wrap.style.display=(mode==="time")?"block":"none";

    if(mode!=="time"){
        document.getElementById("historyTime").value="";
    }
}

/* 覆盖物管理 */

function clearQueryMarker(){
    if(historyQueryMarker && map){
        map.remove(historyQueryMarker);
        historyQueryMarker=null;
    }

    if(queryInfoWindow){
        queryInfoWindow.close();
        queryInfoWindow=null;
    }
}

function clearHistory(){
    const overlays=[];

    if(historyPolyline)overlays.push(historyPolyline);
    if(historyStartMarker)overlays.push(historyStartMarker);
    if(historyEndMarker)overlays.push(historyEndMarker);
    if(historyQueryMarker)overlays.push(historyQueryMarker);

    if(overlays.length>0 && map){
        map.remove(overlays);
    }

    historyPolyline=null;
    historyStartMarker=null;
    historyEndMarker=null;
    historyQueryMarker=null;

    if(queryInfoWindow){
        queryInfoWindow.close();
        queryInfoWindow=null;
    }
}

/* 历史轨迹绘制：有红色终点 */

function drawHistoryTrack(path){
    historyPolyline=new AMap.Polyline({
        path:path,
        strokeColor:"#1677ff",
        strokeWeight:6,
        strokeOpacity:0.85,
        lineJoin:"round",
        lineCap:"round"
    });

    historyStartMarker=new AMap.Marker({
        position:path[0],
        content:startMarkerContent,
        anchor:"center"
    });

    historyEndMarker=new AMap.Marker({
        position:path[path.length-1],
        content:endMarkerContent,
        anchor:"center"
    });

    map.add([
        historyPolyline,
        historyStartMarker,
        historyEndMarker
    ]);

    map.setFitView([
        historyPolyline,
        historyStartMarker,
        historyEndMarker
    ]);
}

/* 今日轨迹绘制：无红色终点 */

function drawTodayTrack(path){
    historyPolyline=new AMap.Polyline({
        path:path,
        strokeColor:"#1677ff",
        strokeWeight:6,
        strokeOpacity:0.85,
        lineJoin:"round",
        lineCap:"round"
    });

    historyStartMarker=new AMap.Marker({
        position:path[0],
        content:startMarkerContent,
        anchor:"center"
    });

    map.add([
        historyPolyline,
        historyStartMarker
    ]);

    if(latestLongitude!==null && latestLatitude!==null && marker){
        marker.setPosition(
            convertWGS84ToGCJ02(latestLongitude,latestLatitude)
        );
        marker.show();
    }

    map.setFitView([
        historyPolyline,
        historyStartMarker
    ]);
}

/* 初始化地图 */

async function initMap(){
    try{
        AMap=await loadMapSdk({
            key:"YOUR_AMAP_WEB_KEY",
            version:"2.0"
        });

        // 先初始化地图，再加载定位
        map=new AMap.Map(
            "container",
            {
                zoom:17,
                center:DEFAULT_MAP_CENTER
            }
        );

        document.getElementById("mapMessage").hidden=true;
        createHistoryDateList();
        updateHistoryTimeVisibility();

        const data=await getLatestLocation();

        if(!data || data.found===false){
            document.getElementById("longitude").textContent="暂无";
            document.getElementById("latitude").textContent="暂无";
            updateGpsTime("");
            updateGpsStatus("暂无GPS数据");
            scheduleRealtimeUpdate();
            return;
        }

        latestLongitude=Number(data.longitude);
        latestLatitude=Number(data.latitude);
        lastLongitude=latestLongitude;
        lastLatitude=latestLatitude;
        if(data.device_id) DEVICE_ID=data.device_id;   // 记录真实设备

        document.getElementById("longitude").textContent=latestLongitude;
        document.getElementById("latitude").textContent=latestLatitude;
        updateGpsTime(data.gps_time);
        updateGpsStatus("正常");

        const mapPoint=convertWGS84ToGCJ02(
            latestLongitude,
            latestLatitude
        );

        map.setZoomAndCenter(17,mapPoint);

        marker=new AMap.Marker({
            position:mapPoint,
            content:realtimeMarkerContent,
            anchor:"bottom-center"
        });

        map.add(marker);

        scheduleRealtimeUpdate();
        console.log("地图初始化完成");
    }
    catch(error){
        console.error("地图初始化失败",error);
        updateGpsStatus(map ? "定位暂时无法读取" : "地图加载失败");
        if(map){
            notifyMap('定位读取失败，正在自动重试；地图可继续查看。');
            scheduleRealtimeUpdate();
        }else showMapFailure();
    }
}

/* 实时定位循环：2秒 */

function scheduleRealtimeUpdate(){
    setTimeout(async function(){
        await updateRealtimeLocation();
        scheduleRealtimeUpdate();
    },REALTIME_REFRESH_INTERVAL);
}

async function updateRealtimeLocation(){
    try{
        const data=await getLatestLocation();

        if(!data || data.found===false){
            updateGpsStatus("暂无GPS数据");
            return;
        }

        const longitude=Number(data.longitude);
        const latitude=Number(data.latitude);

        if(!Number.isFinite(longitude) || !Number.isFinite(latitude)){
            throw new Error("后端返回的经纬度无效");
        }

        latestLongitude=longitude;
        latestLatitude=latitude;
        if(data.device_id) DEVICE_ID=data.device_id;

        document.getElementById("longitude").textContent=longitude;
        document.getElementById("latitude").textContent=latitude;
        updateGpsTime(data.gps_time);
        updateGpsStatus("正常");

        const mapPoint=convertWGS84ToGCJ02(longitude,latitude);

        if(!marker){
            marker=new AMap.Marker({
                position:mapPoint,
                content:realtimeMarkerContent,
                anchor:"bottom-center"
            });
            map.add(marker);
        }else{
            marker.setPosition(mapPoint);
        }

        // 历史模式隐藏当前定位点
        if(currentMode==="realtime" || currentMode==="today"){
            marker.show();
        }else{
            marker.hide();
        }

        const distance=(lastLongitude===null || lastLatitude===null)?0:
            calculateDistance(
                lastLongitude,
                lastLatitude,
                longitude,
                latitude
            );


        // 实时位置变化超过 5km 时重置中心
        if(currentMode==="realtime" && distance>=RELOCATE_DISTANCE){
            map.setCenter(mapPoint);
        }

        lastLongitude=longitude;
        lastLatitude=latitude;
    }
    catch(error){
        console.error("实时更新失败",error);
        updateGpsStatus("暂时无法获取："+error.message);
    }
}

/* 今日轨迹 */

async function showTodayTrack(){
    if(currentMode==="today"){
        returnRealtime();
        return;
    }

    try{
        document.getElementById("mapFeedback").hidden=true;
        currentMode="today";
        currentHistoryDate=null;
        showHistoryPanel(false);
        showTodayQueryPanel(true);
        clearHistory();
        updateModeStatus("今日轨迹");

        const data=await getTodayTrack();

        if(!data.locations || data.locations.length===0){
            if(marker)marker.show();
            notifyMap("今日暂无GPS数据");
            return;
        }

        const path=data.locations.map(locationToMapPoint);

        drawTodayTrack(path);
        console.log("今日轨迹加载完成",data.count);
    }
    catch(error){
        console.error("今日轨迹失败",error);
        notifyMap("今日轨迹加载失败："+error.message);
    }
}

/* 今日具体时刻查询 */

async function queryTodayTime(){
    const timeValue=document.getElementById("todayTime").value;

    if(!timeValue){
        clearQueryMarker();
        return;
    }

    await addQueryMarker(
        getTodayDateString(),
        normalizeTimeString(timeValue)
    );
}

/* 历史轨迹 */

async function queryHistory(){
    try{
        document.getElementById("mapFeedback").hidden=true;
        const date=getSelectedHistoryDate();

        if(!date || date==="__custom__"){
            notifyMap("请选择有效的历史日期");
            return;
        }

        currentMode="history";
        currentHistoryDate=date;
        showTodayQueryPanel(false);
        showHistoryPanel(true);
        clearHistory();
        if(marker)marker.hide();
        updateModeStatus("历史轨迹");

        const data=await getHistoryByDate(date);

        if(!data.locations || data.locations.length===0){
            notifyMap("该日期暂无GPS数据");
            return;
        }

        // 历史轨迹转换为 GCJ-02 后绘制

        const path=data.locations.map(locationToMapPoint);

        drawHistoryTrack(path);

        // 指定时刻时显示查询标记
        const timeMode=document.getElementById("historyTimeMode").value;
        const queryTime=document.getElementById("historyTime").value;

        if(timeMode==="time" && queryTime){
            await addQueryMarker(
                date,
                normalizeTimeString(queryTime)
            );
        }
    }
    catch(error){
        console.error("历史查询失败",error);
        notifyMap("历史轨迹查询失败："+error.message);
    }
}

/* 指定时间附近最近 GPS 点 */

async function addQueryMarker(date,time){
    try{
        const result=await getNearestLocation(
            date,
            normalizeTimeString(time)
        );

        if(!result.found){
            clearQueryMarker();
            notifyMap(result.message || "该时间附近5分钟没有GPS记录");
            return;
        }

        clearQueryMarker();

        const mapPoint=convertWGS84ToGCJ02(
            Number(result.longitude),
            Number(result.latitude)
        );

        historyQueryMarker=new AMap.Marker({
            position:mapPoint,
            content:queryMarkerContent,
            anchor:"center"
        });

        map.add(historyQueryMarker);

        queryInfoWindow=new AMap.InfoWindow({
            content:`
                <div style="width:270px;font-size:14px;line-height:22px;">
                    <b>查询时间（北京时间）:</b> ${result.query_time}<br>
                    <b>GPS时间（北京时间）:</b> ${result.gps_time}<br>
                    <b>相差:</b> ${result.difference_seconds} 秒<br>
                    <b>GPS经度（WGS84）:</b> ${result.longitude}<br>
                    <b>GPS纬度（WGS84）:</b> ${result.latitude}<br>
                    <span style="color:#666;">地图显示位置已转换为 GCJ-02</span>
                </div>
            `,
            offset:new AMap.Pixel(0,-35)
        });

        historyQueryMarker.on("click",function(){
            queryInfoWindow.open(
                map,
                historyQueryMarker.getPosition()
            );
        });
    }
    catch(error){
        console.error("时间查询失败",error);
        notifyMap("时间查询失败："+error.message);
    }
}

/* 返回实时定位 */

function returnRealtime(){
    document.getElementById("mapFeedback").hidden=true;
    currentMode="realtime";
    currentHistoryDate=null;
    clearHistory();
    showHistoryPanel(false);
    showTodayQueryPanel(false);
    updateModeStatus("实时定位");

    document.getElementById("todayTime").value="";
    document.getElementById("historyTime").value="";
    document.getElementById("historyTimeMode").value="";
    updateHistoryTimeVisibility();

    if(marker)marker.show();

    if(
        latestLongitude!==null &&
        latestLatitude!==null &&
        map
    ){
        map.setZoomAndCenter(
            17,
            convertWGS84ToGCJ02(
                latestLongitude,
                latestLatitude
            )
        );
    }
}

/* 按钮与输入事件 */

document.getElementById("todayTrackButton").onclick=showTodayTrack;

document.getElementById("historyButton").onclick=function(){
    const panel=document.getElementById("historyPanel");

    if(currentMode==="history" && panel.style.display==="block"){
        // 再次点击返回实时定位
        returnRealtime();
        return;
    }

    currentMode="history";
    currentHistoryDate=null;
    showTodayQueryPanel(false);
    showHistoryPanel(true);
    if(marker)marker.hide();
    updateModeStatus("历史轨迹查询");
};

document.getElementById("historyDateSelect").addEventListener("change",function(){
    updateCustomDateVisibility();
});

document.getElementById("historyTimeMode").addEventListener("change",function(){
    updateHistoryTimeVisibility();

    // 未指定时刻时清除查询标记

    if(this.value!=="time"){
        clearQueryMarker();
    }
});

document.getElementById("queryHistoryButton").onclick=queryHistory;
document.getElementById("queryTodayButton").onclick=queryTodayTime;
document.getElementById("realtimeButton").onclick=returnRealtime;

async function loadMapSdk(options){
    if(typeof AMapLoader==='undefined') throw new Error('地图服务未加载');
    let timer;
    try{return await Promise.race([AMapLoader.load(options),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('地图加载超时')),15000);})]);}
    finally{clearTimeout(timer);}
}
async function openLinkedEvent(){
    const time=new URLSearchParams(location.search).get('event_time');
    if(!time || !/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(time) || !map) return;
    currentMode='history';
    currentHistoryDate=time.slice(0,10);
    showHistoryPanel(true);showTodayQueryPanel(false);updateModeStatus('报警附近定位');
    document.getElementById('historyDateSelect').value='__custom__';
    updateCustomDateVisibility();document.getElementById('customDate').max=getTodayDateString();document.getElementById('customDate').value=time.slice(0,10);
    document.getElementById('historyTimeMode').value='time';updateHistoryTimeVisibility();
    document.getElementById('historyTime').value=time.slice(11);
    await addQueryMarker(time.slice(0,10),time.slice(11));
    if(historyQueryMarker) map.setZoomAndCenter(17,historyQueryMarker.getPosition());
    notifyMap('按报警记录的接收时间查询附近定位；离线补发时可能与实际发生位置不同。');
}
document.getElementById('centerPosition').onclick=function(){
    if(!map || latestLongitude===null || latestLatitude===null){notifyMap('尚无有效位置，请等待设备上传定位。');return;}
    map.setZoomAndCenter(17,convertWGS84ToGCJ02(latestLongitude,latestLatitude));
};
// 查询期间禁用按钮
['todayTrackButton','queryHistoryButton','queryTodayButton'].forEach(function(id){
    const button=document.getElementById(id),action=button.onclick;
    button.onclick=async function(){
        if(button.disabled)return;
        button.disabled=true;button.setAttribute('aria-busy','true');
        try{await action.call(button);}finally{button.disabled=false;button.removeAttribute('aria-busy');}
    };
});
setInterval(updateFreshness,1000);
window.addEventListener('load',async function(){await initMap();await openLinkedEvent();});

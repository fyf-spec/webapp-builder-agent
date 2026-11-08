"""A new inventory application composed from B6 helpers; no benchmark artifacts."""
BUSINESS = '''import type {Express} from "express";
import type Database from "better-sqlite3";
import {DocumentStore} from "../runtime/store.js";
import {endpoint,HttpError,requiredText} from "../runtime/http.js";
export function initializeBusiness(db:Database.Database){new DocumentStore(db);}
export function registerBusiness(app:Express,db:Database.Database){
 const store=new DocumentStore(db);
 app.get('/api/items',endpoint((_q,r)=>{r.json(store.list('items'));}));
 app.post('/api/items',endpoint((q,r)=>{r.status(201).json(store.create('items',{name:requiredText(q.body.name,'Name',100)}));}));
 app.get('/api/items/:id',endpoint((q,r)=>{const value=store.get('items',String(q.params.id));
   if(!value)throw new HttpError(404,'Item not found');r.json(value);}));
 app.patch('/api/items/:id',endpoint((q,r)=>{const value=store.update('items',String(q.params.id),{name:requiredText(q.body.name,'Name',100)});
   if(!value)throw new HttpError(404,'Item not found');r.json(value);}));
 app.delete('/api/items/:id',endpoint((q,r)=>{if(!store.delete('items',String(q.params.id)))throw new HttpError(404,'Item not found');r.status(204).end();}));
}
'''

APP = '''import React,{useEffect,useState} from "react";
import {api} from "./runtime/api";
import {Layout,Field,Notice,DataTable,ConfirmDialog,Link,useLocation,useResource,navigate} from "./runtime/ui";
type Item={id:string,data:{name:string}};
export function App(){
 const route=useLocation(),id=route.path.startsWith('/items/')?decodeURIComponent(route.path.slice(7)):null;
 const items=useResource<Item[]>('/api/items'),detail=useResource<Item>(id?'/api/items/'+encodeURIComponent(id):null);
 const [name,setName]=useState(''),[error,setError]=useState(''),[deleting,setDeleting]=useState<Item|null>(null);
 useEffect(()=>{setName(detail.data?.data.name??'');setError('');},[detail.data,route.path]);
 async function save(e:React.FormEvent){e.preventDefault();setError('');try{
  await api(id?'/api/items/'+encodeURIComponent(id):'/api/WRONG',{method:id?'PATCH':'POST',json:{name}});
  setName('');items.reload();if(id)navigate('/');
 }catch(e){setError((e as Error).message);}}
 async function remove(){if(!deleting)return;try{await api('/api/items/'+encodeURIComponent(deleting.id),{method:'DELETE'});
  setDeleting(null);items.reload();}catch(e){setError((e as Error).message);}}
 return <Layout title="Inventory" navigation={<Link href="/">Items</Link>}>
  <h1>{id?'Edit item':'Items'}</h1><Notice error>{error||items.error||detail.error}</Notice>
  {id&&(!detail.data||detail.loading)?<p role="status">Loading item</p>:<form onSubmit={save}><Field label="Name"><input value={name} onChange={e=>setName(e.target.value)}/></Field>
  <button type="submit">{id?'Save':'Create'}</button>{id&&<Link href="/">Cancel</Link>}</form>}
  {!id&&<DataTable rows={items.data??[]} rowKey={row=>row.id} columns={[
   {key:'name',title:'Name',render:row=><Link href={'/items/'+encodeURIComponent(row.id)}>{row.data.name}</Link>},
   {key:'actions',title:'Actions',render:row=><button onClick={()=>setDeleting(row)}>Delete</button>}
  ]}/>}
  <ConfirmDialog open={!!deleting} title="Delete item" confirmLabel="Confirm delete" cancelLabel="Cancel"
   onCancel={()=>setDeleting(null)} onConfirm={()=>void remove()}><p>{deleting?.data.name}</p></ConfirmDialog>
 </Layout>;
}
'''

BLUE = "page.getByRole('link',{name:'Blue pen',exact:true})"
RED = "page.getByRole('link',{name:'Red pen',exact:true})"
DELETE = "page.getByRole('button',{name:'Delete',exact:true})"
NAME = "page.getByLabel('Name',{exact:true})"
EMPTY = "page.getByText('No items',{exact:true})"
CREATE = "page.getByRole('button',{name:'Create',exact:true})"
CANCEL = "page.getByRole('link',{name:'Cancel',exact:true})"
DIALOG = "page.getByRole('dialog')"
IDS = ['STEP-CREATE', 'STEP-EDIT', 'STEP-DELETE']


def checkpoint(step, body, target, matcher="toBeVisible", *args):
    return {'id': step, 'body': body, 'target': target, 'matcher': matcher, 'args': list(args)}


ACCEPTANCE = {'cases': [{'title': 'Create, edit, cancel and delete an item', 'steps': [
    checkpoint('STEP-CREATE', "await page.goto('/');", EMPTY),
    checkpoint('STEP-CREATE', f"await {CREATE}.click();", "page.getByRole('alert')", 'toHaveText', 'Name is required'),
    checkpoint('STEP-CREATE', f"await {NAME}.fill('Blue pen'); await {CREATE}.click();", BLUE),
    checkpoint('STEP-CREATE', "await page.reload();", BLUE),
    checkpoint('STEP-EDIT', f"await {BLUE}.click();", NAME, "toHaveValue", "Blue pen"),
    checkpoint('STEP-EDIT', f"await {NAME}.fill('Unwanted'); await {CANCEL}.click(); "
               "await page.waitForURL('**/'); await page.reload();", BLUE),
    checkpoint('STEP-EDIT', f"await {BLUE}.click(); await {NAME}.fill('Red pen'); "
               "await page.getByRole('button',{name:'Save',exact:true}).click();", RED),
    checkpoint('STEP-EDIT', "await page.reload();", RED),
    checkpoint('STEP-DELETE', "", DELETE),
    checkpoint('STEP-DELETE', f"await {DELETE}.click(); await {DIALOG}.getByRole('button',{{name:'Cancel',exact:true}}).click();", RED),
    checkpoint('STEP-DELETE', f"await {DELETE}.click(); await {DIALOG}.getByRole('button',{{name:'Confirm delete',exact:true}}).click();", EMPTY),
    checkpoint('STEP-DELETE', "await page.reload();", EMPTY),
]}]}
